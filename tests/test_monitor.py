from __future__ import annotations

import asyncio
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_pixiv_novel_sub.monitor import (
    ENDPOINT,
    REFERER,
    Options,
    PixivClient,
    PixivError,
    StateError,
    StateStore,
    Watcher,
    parse_response,
)


def payload(ids):
    return {
        "error": False,
        "message": "",
        "body": {
            "page": {"ids": list(ids), "isLastPage": False},
            "thumbnails": {
                "novel": [
                    {
                        "id": key,
                        "title": f"标题 {key}",
                        "userName": "作者",
                        "createDate": "2026-10-14T20:00:00+08:00",
                    }
                    for key in ids
                ]
            },
        },
    }


class FakeClient:
    def __init__(self):
        self.options = Options(cookie="PHPSESSID=123_test")
        self.response = payload(["2", "1"])
        self.error = None

    async def fetch(self):
        await asyncio.sleep(0)
        if self.error:
            raise self.error
        return copy.deepcopy(self.response), parse_response(self.response)


class WatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = StateStore(Path(self.temp.name) / "state.json")
        self.client = FakeClient()
        self.watcher = Watcher(self.client, self.store)

    async def test_first_success_is_baseline_and_persists_response(self):
        result = await self.watcher.check()
        self.assertTrue(result.baseline)
        self.assertEqual(result.count, 2)
        self.assertFalse(result.new)
        state = self.store.load()
        self.assertEqual(state["response"], self.client.response)
        self.assertEqual(state["latest_ids"], ["2", "1"])
        self.assertNotIn("PHPSESSID", self.store.path.read_text(encoding="utf-8"))
        self.assertNotIn("subscriptions", state)
        self.assertNotIn("pending", state)

    async def test_new_ids_only_and_reordering_editing_not_new(self):
        await self.watcher.check()
        self.client.response = payload(["3", "2", "1", "3"])
        self.assertEqual(
            [item["id"] for item in (await self.watcher.check()).new], ["3"]
        )
        self.client.response = payload(["1", "3", "2"])
        self.client.response["body"]["thumbnails"]["novel"][0]["title"] = "改名"
        self.assertFalse((await self.watcher.check()).new)

    async def test_restart_and_reappearing_work_not_duplicated(self):
        await self.watcher.check()
        self.client.response = payload(["3"])
        await self.watcher.check()
        self.watcher = Watcher(self.client, self.store)
        self.client.response = payload(["2", "3"])
        result = await self.watcher.check()
        self.assertFalse(result.baseline)
        self.assertFalse(result.new)

    async def test_failed_request_preserves_previous_response(self):
        await self.watcher.check()
        previous = self.store.path.read_bytes()
        self.client.error = PixivError("请求失败")
        with self.assertRaises(PixivError):
            await self.watcher.check()
        self.assertEqual(self.store.path.read_bytes(), previous)
        self.client.error = None
        self.client.response = payload(["4", "2"])
        self.assertEqual((await self.watcher.check()).new[0]["id"], "4")

    async def test_first_failure_not_initialized_and_empty_feed_valid(self):
        self.client.error = PixivError("失败")
        with self.assertRaises(PixivError):
            await self.watcher.check()
        self.assertFalse(self.watcher.state["initialized"])
        self.client.error = None
        self.client.response = payload([])
        self.assertTrue((await self.watcher.check()).baseline)
        self.client.response = payload(["1"])
        self.assertEqual((await self.watcher.check()).new[0]["id"], "1")

    async def test_commit_failure_does_not_consume_updates(self):
        await self.watcher.check()
        self.client.response = payload(["3"])
        original = copy.deepcopy(self.watcher.state)
        with (
            patch.object(self.store, "save", side_effect=StateError("磁盘已满")),
            self.assertRaises(StateError),
        ):
            await self.watcher.check()
        self.assertEqual(self.watcher.state, original)
        self.assertEqual((await self.watcher.check()).new[0]["id"], "3")

    async def test_concurrent_checks_only_report_update_once(self):
        await self.watcher.check()
        self.client.response = payload(["3"])
        results = await asyncio.gather(self.watcher.check(), self.watcher.check())
        self.assertEqual(sum(len(result.new) for result in results), 1)

    async def test_cookie_account_change_rebaselines(self):
        await self.watcher.check()
        self.client.options = Options(cookie="PHPSESSID=456_new")
        self.client.response = payload(["8"])
        result = await self.watcher.check()
        self.assertTrue(result.baseline)
        self.assertFalse(result.new)
        self.assertEqual(self.watcher.state["seen_ids"], ["8"])

    async def test_same_account_session_renewal_keeps_baseline(self):
        await self.watcher.check()
        self.client.options = Options(cookie="PHPSESSID=123_renewed")
        self.client.response = payload(["3"])
        result = await self.watcher.check()
        self.assertFalse(result.baseline)
        self.assertEqual(result.new[0]["id"], "3")

    async def test_atomic_replace_failure_keeps_disk_state(self):
        await self.watcher.check()
        previous = self.store.path.read_bytes()
        candidate = copy.deepcopy(self.watcher.state)
        candidate["seen_ids"].append("999")
        with (
            patch(
                "astrbot_plugin_pixiv_novel_sub.monitor.os.replace", side_effect=OSError
            ),
            self.assertRaises(StateError),
        ):
            self.watcher.commit(candidate)
        self.assertEqual(self.store.path.read_bytes(), previous)
        self.assertNotIn("999", self.watcher.state["seen_ids"])

    async def test_migrate_old_state_preserving_baseline(self):
        await self.watcher.check()
        old = copy.deepcopy(self.watcher.state)
        old["version"] = 1
        old["responses"] = [{"page": 1, "response": old.pop("response")}]
        old.update(
            subscriptions=["bot:GroupMessage:1"], pending={"bot:GroupMessage:1": []}
        )
        self.store.save(old)
        self.watcher = Watcher(self.client, self.store)
        self.client.response = payload(["3", "2"])
        result = await self.watcher.check()
        self.assertFalse(result.baseline)
        self.assertEqual([novel["id"] for novel in result.new], ["3"])
        state = self.store.load()
        self.assertEqual(state["version"], 2)
        self.assertNotIn("subscriptions", state)
        self.assertNotIn("pending", state)


class ParserTests(unittest.TestCase):
    def test_extra_thumbnails_excluded(self):
        data = payload(["1"])
        data["body"]["thumbnails"]["novel"].append({"id": "999", "title": "推荐"})
        self.assertEqual([novel["id"] for novel in parse_response(data)], ["1"])

    def test_response_without_page_ids(self):
        data = payload(["1"])
        del data["body"]["page"]["ids"]
        self.assertEqual(parse_response(data)[0]["id"], "1")

    def test_invalid_payloads_rejected(self):
        bad = [
            None,
            [],
            {},
            {"error": True, "message": "secret"},
            {"error": False, "body": {}},
            payload(["bad"]),
            payload([True]),
        ]
        incomplete = payload(["1"])
        incomplete["body"]["page"]["ids"].append("2")
        bad.append(incomplete)
        for data in bad:
            with self.subTest(data=data), self.assertRaises(PixivError) as raised:
                parse_response(data)
            self.assertNotIn("secret", str(raised.exception))

    def test_cookie_account_inferred_without_user_id_option(self):
        self.assertEqual(
            Options(cookie="a=b; PHPSESSID=123_token").cookie_account, "123"
        )
        self.assertNotIn("user_id", Options.__dataclass_fields__)

    def test_corrupt_state_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            for content in ("{", json.dumps({"version": 100}), "null"):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(StateError):
                    StateStore(path).load()
                self.assertEqual(path.read_text(encoding="utf-8"), content)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler, cookie="PHPSESSID=123_test; cf_clearance=test"):
        client = PixivClient(Options(cookie=cookie))
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            trust_env=False,
            headers={"Accept-Encoding": "gzip, deflate"},
        )
        self.addAsyncCleanup(client.close)
        return client

    async def test_fixed_request_and_current_cookie_account(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=payload(["1"]))

        response, novels = await self.client(handler).fetch()
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(str(request.url), ENDPOINT + "?p=1&mode=all&lang=zh")
        self.assertEqual(request.headers["referer"], REFERER)
        self.assertEqual(request.headers["x-user-id"], "123")
        self.assertEqual(request.headers["accept-encoding"], "gzip, deflate")
        self.assertIn("PHPSESSID=123_test", request.headers["cookie"])
        self.assertEqual(novels[0]["id"], "1")
        self.assertEqual(response, payload(["1"]))

    async def test_auth_html_and_api_errors_are_safe(self):
        for response in (
            httpx.Response(403),
            httpx.Response(302, headers={"Location": "/login"}),
            httpx.Response(200, text="<html>PHPSESSID=secret</html>"),
            httpx.Response(200, json={"error": True, "message": "PHPSESSID=secret"}),
        ):
            with (
                self.subTest(status=response.status_code),
                self.assertRaises(PixivError) as raised,
            ):
                await self.client(lambda request, result=response: result).fetch()
            self.assertNotIn("secret", str(raised.exception))

    async def test_transient_failure_retried(self):
        calls = []

        def handler(request):
            calls.append(request)
            return (
                httpx.Response(503)
                if len(calls) == 1
                else httpx.Response(200, json=payload(["1"]))
            )

        with patch(
            "astrbot_plugin_pixiv_novel_sub.monitor.asyncio.sleep", new=AsyncMock()
        ):
            await self.client(handler).fetch()
        self.assertEqual(len(calls), 2)

    async def test_missing_cookie_does_not_request(self):
        handler = unittest.mock.Mock()
        with self.assertRaises(PixivError):
            await self.client(handler, cookie="").fetch()
        handler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
