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
    def __init__(self, admin=True, sender="7"):
        self.admin, self.sender, self.stopped = admin, sender, False

    def is_admin(self):
        return self.admin

    def get_sender_id(self):
        return self.sender

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text


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

    async def test_only_query_tools_registered_and_permissions_enforced(self):
        event = FakeEvent(admin=False)
        for method in (self.plugin.check_tool, self.plugin.status_tool):
            self.assertTrue(method.registration["name"].startswith("pixiv_novel_"))
            self.assertIn("仅管理员", await method(event))
        self.assertFalse(self.plugin.watcher.state["initialized"])
        self.assertFalse(event.stopped)
        self.assertFalse(hasattr(self.plugin, "subscribe_tool"))
        self.assertFalse(hasattr(self.plugin, "unsubscribe_tool"))

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
            set(schema), {"cookie", "network_proxy", "admin_only", "allowed_users"}
        )
        self.assertTrue(schema["cookie"]["secret"])

    async def test_terminate_closes_client(self):
        self.plugin.client.close = AsyncMock()
        await self.plugin.terminate()
        self.plugin.client.close.assert_awaited_once()
        self.assertIn("正在停止", await self.plugin.check_tool(FakeEvent()))


if __name__ == "__main__":
    unittest.main()
