from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_monitor import FakeClient, payload

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class FakeStar:
    def __init__(self, context):
        self.context = context


class FakeStarTools:
    path = None

    @classmethod
    def get_data_dir(cls, name):
        return cls.path


def decorator(**kwargs):
    def wrap(function):
        function.registration = kwargs
        return function

    return wrap


api = types.ModuleType("astrbot.api")
api.AstrBotConfig, api.logger = dict, logging.getLogger("pixiv-test")
event_api = types.ModuleType("astrbot.api.event")
event_api.AstrMessageEvent = object
event_api.filter = types.SimpleNamespace(
    command=lambda name, **kwargs: decorator(command=name, **kwargs), llm_tool=decorator
)
star_api = types.ModuleType("astrbot.api.star")
star_api.Context, star_api.Star, star_api.StarTools = object, FakeStar, FakeStarTools
with patch.dict(
    sys.modules,
    {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": api,
        "astrbot.api.event": event_api,
        "astrbot.api.star": star_api,
    },
):
    plugin_module = importlib.import_module("astrbot_plugin_pixiv_novel_sub.main")


class FakeEvent:
    unified_msg_origin = "bot:GroupMessage:1"

    def __init__(self, admin=True, sender="7"):
        self.admin, self.sender, self.stopped = admin, sender, False
        self.sent = []

    def is_admin(self):
        return self.admin

    def get_sender_id(self):
        return self.sender

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text

    async def send(self, text):
        self.sent.append(text)


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        FakeStarTools.path = Path(self.temp.name)
        self.context = types.SimpleNamespace()
        self.config = {"cookie": "PHPSESSID=123_test"}
        self.plugin = plugin_module.PixivNovelSubPlugin(self.context, self.config)
        self.client = FakeClient()
        self.plugin.client.fetch = self.client.fetch
        self.addAsyncCleanup(self.plugin.terminate)

    async def test_tools_registered_and_permissions_enforced(self):
        event = FakeEvent(admin=False)
        for method in (self.plugin.check_tool, self.plugin.status_tool):
            self.assertTrue(method.registration["name"].startswith("pixiv_novel_"))
            self.assertIn("仅管理员", await method(event))
        self.assertFalse(self.plugin.watcher.state["initialized"])
        self.assertFalse(event.stopped)
        self.assertFalse(hasattr(self.plugin, "subscribe_tool"))
        self.assertFalse(hasattr(self.plugin, "unsubscribe_tool"))
        self.assertIn(
            "仅管理员",
            await self.plugin.download_tool(
                event, "https://www.pixiv.net/novel/show.php?id=1"
            ),
        )
        self.assertIn("仅管理员", await self.plugin.download_status_tool(event))
        self.assertFalse(self.plugin.download_jobs)

    async def test_whitelist_can_check_without_subscription(self):
        self.config["allowed_users"] = ["7"]
        event = FakeEvent(admin=False)
        self.assertIn("首次比较基线", await self.plugin.check_tool(event))
        self.client.response = payload(["3", "2"])
        self.assertIn("show.php?id=3", await self.plugin.check_tool(event))
        self.assertFalse(event.stopped)

    async def test_command_stops_event_tools_do_not(self):
        event = FakeEvent()
        results = [item async for item in self.plugin.check_command(event)]
        self.assertTrue(event.stopped)
        self.assertIn("首次比较基线", results[0])
        event = FakeEvent()
        await self.plugin.status_tool(event)
        self.assertFalse(event.stopped)

    async def test_all_new_works_returned_to_caller(self):
        await self.plugin.check_tool(FakeEvent())
        self.client.response = payload([str(key) for key in range(3, 15)])
        result = await self.plugin.check_tool(FakeEvent())
        self.assertIn("12 篇新增", result)
        for key in range(3, 15):
            self.assertIn(f"show.php?id={key}", result)

    async def test_constructor_does_not_start_background_requests(self):
        self.plugin.client.fetch = AsyncMock()
        await asyncio.sleep(0)
        self.plugin.client.fetch.assert_not_awaited()
        self.assertFalse(hasattr(self.plugin, "worker"))
        self.assertFalse(hasattr(self.plugin, "poll"))

    async def test_status_has_no_credentials_or_subscription_details(self):
        await self.plugin.check_tool(FakeEvent())
        text = self.plugin.describe_status(FakeEvent())
        self.assertIn("上次成功检查", text)
        self.assertNotIn("123_test", text)
        self.assertNotIn("订阅", text)
        self.assertNotIn("定时", text)

    async def test_removed_configuration_fields_absent(self):
        path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(path.read_text(encoding="utf-8-sig"))
        self.assertEqual(
            set(schema),
            {"cookie", "network_proxy", "admin_only", "allowed_users", "save_path"},
        )
        self.assertTrue(schema["cookie"]["secret"])

    async def test_terminate_closes_client(self):
        self.plugin.client.close = AsyncMock()
        await self.plugin.terminate()
        self.plugin.client.close.assert_awaited_once()
        self.assertIn("正在停止", await self.plugin.check_tool(FakeEvent()))

    async def test_download_tool_runs_job_and_sends_text_completion_only(self):
        result = plugin_module.DownloadResult(
            Path(self.temp.name) / "作品_作者", "作品", "作者", 1
        )
        self.plugin.downloader.download = AsyncMock(return_value=result)
        event = FakeEvent()
        submitted = await self.plugin.download_tool(
            event, "https://www.pixiv.net/novel/show.php?id=28347721"
        )
        self.assertIn("已提交", submitted)
        self.assertFalse(event.stopped)
        job = next(iter(self.plugin.download_jobs.values()))
        await job.task
        self.assertEqual(job.stage, "已完成")
        self.assertEqual(len(event.sent), 1)
        self.assertIsInstance(event.sent[0], str)
        self.assertIn("下载完成", event.sent[0])
        self.assertIn("metadata.json", event.sent[0])
        self.assertIn("已完成", await self.plugin.download_status_tool(event, job.id))

    async def test_failed_download_does_not_notify_completion(self):
        self.plugin.downloader.download = AsyncMock(
            side_effect=plugin_module.DownloadError("未返回正文")
        )
        event = FakeEvent()
        await self.plugin.download_tool(
            event, "https://www.pixiv.net/novel/show.php?id=1"
        )
        job = next(iter(self.plugin.download_jobs.values()))
        await job.task
        self.assertEqual(job.stage, "失败")
        self.assertIn("失败", event.sent[0])
        self.assertNotIn("下载完成", event.sent[0])

    async def test_download_duplicate_submission_and_session_visibility(self):
        pending = asyncio.Event()

        async def wait(*args):
            await pending.wait()

        self.plugin.downloader.download = wait
        event = FakeEvent()
        await self.plugin.download_tool(event, "https://www.pixiv.net/novel/series/1")
        self.assertIn(
            "已有下载任务",
            await self.plugin.download_tool(
                event, "https://www.pixiv.net/novel/series/1"
            ),
        )
        other = FakeEvent()
        other.unified_msg_origin = "bot:GroupMessage:2"
        self.assertIn("没有可查看", await self.plugin.download_status_tool(other))
        job = next(iter(self.plugin.download_jobs.values()))
        await self.plugin.terminate()
        self.assertTrue(job.task.done())

    async def test_download_command_and_configured_save_path(self):
        destination = Path(self.temp.name) / "saved"
        self.config["save_path"] = str(destination)
        plugin = plugin_module.PixivNovelSubPlugin(self.context, self.config)
        self.addAsyncCleanup(plugin.terminate)
        self.assertEqual(plugin.download_root, destination.resolve())
        event = FakeEvent()
        result = [
            item
            async for item in plugin.download_command(event, "https://example.com/bad")
        ]
        self.assertTrue(event.stopped)
        self.assertIn("请提供 Pixiv", result[0])
        self.assertFalse(plugin.download_jobs)


if __name__ == "__main__":
    unittest.main()
