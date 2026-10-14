from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_pixiv_novel_sub.downloader import (
    DownloadError,
    NovelDownloader,
    Target,
    safe_name,
)
from astrbot_plugin_pixiv_novel_sub.monitor import Options, PixivClient


def novel(key, *, title=None, series=False):
    body = {
        "id": str(key),
        "title": title or f"章节 {key}",
        "userName": "作者",
        "userId": "123",
        "content": f"正文 {key}\n[chapter:小节]\n[newpage]\n下一页",
        "createDate": "2026-10-14T20:00:00+08:00",
        "description": "章节说明",
        "tags": {"tags": [{"tag": "测试"}]},
    }
    if series:
        body["seriesNavData"] = {"seriesId": "15917685", "title": "系列名称"}
    return body


def catalogue(orders, *, last=None, title=None):
    page = {
        "seriesContents": [
            {
                "id": str(order),
                "title": f"章节 {order}",
                "series": {"contentOrder": order},
            }
            for order in orders
        ]
    }
    if last is not None:
        page["isLastPage"] = last
    return {
        "page": page,
        "thumbnails": {
            "novel": [{"id": str(order), "seriesTitle": title} for order in orders]
        },
    }


class TargetTests(unittest.TestCase):
    def test_both_user_links(self):
        self.assertEqual(
            Target.parse("https://www.pixiv.net/novel/series/15917685"),
            Target("series", "15917685"),
        )
        self.assertEqual(
            Target.parse("https://www.pixiv.net/novel/show.php?id=28347721"),
            Target("novel", "28347721"),
        )

    def test_invalid_and_foreign_links_rejected(self):
        for url in (
            "http://www.pixiv.net/novel/show.php?id=1",
            "https://example.com/novel/show.php?id=1",
            "https://www.pixiv.net.evil.test/novel/series/1",
            "https://user@www.pixiv.net/novel/series/1",
            "https://www.pixiv.net/novel/show.php?id=1&id=2",
            "https://www.pixiv.net/novel/series/../1",
            "https://www.pixiv.net/novel/show.php?id=0",
            "28347721",
        ):
            with self.subTest(url=url), self.assertRaises(DownloadError):
                Target.parse(url)

    def test_windows_names_and_traversal_sanitized(self):
        self.assertEqual(safe_name('A/B:C*D?E"F<G>H|I\\J'), "A_B_C_D_E_F_G_H_I_J")
        self.assertEqual(safe_name("CON"), "_CON")
        self.assertEqual(safe_name(" .. "), "未命名")
        self.assertLessEqual(len(safe_name("长" * 500)), 80)


class DownloaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "downloads"
        self.client = PixivClient(Options(cookie="PHPSESSID=123_test"))
        self.downloader = NovelDownloader(self.client)
        pause = patch(
            "astrbot_plugin_pixiv_novel_sub.downloader.asyncio.sleep", new=AsyncMock()
        )
        pause.start()
        self.addCleanup(pause.stop)
        self.addAsyncCleanup(self.client.close)
        self.requests = []

    def transport(self, handler):
        def route(request):
            self.requests.append(request)
            body = handler(request)
            return httpx.Response(200, json={"error": False, "body": body})

        self.client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(route), trust_env=False
        )

    async def test_single_novel_layout_metadata_and_complete_content(self):
        data = novel("28347721", title="小说名")
        self.transport(lambda request: data)
        result = await self.downloader.download(Target("novel", "28347721"), self.root)
        self.assertEqual(result.folder.name, "小说名_作者")
        self.assertEqual(result.chapters, 1)
        self.assertEqual(
            (result.folder / "第1章 小说名.txt").read_text(encoding="utf-8"),
            data["content"],
        )
        metadata = json.loads(
            (result.folder / "metadata.json").read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["status"], "complete")
        self.assertEqual(
            metadata["source_url"], "https://www.pixiv.net/novel/show.php?id=28347721"
        )
        self.assertEqual(metadata["saved_chapters"], 1)
        self.assertNotIn("PHPSESSID", json.dumps(metadata))
        self.assertEqual(
            str(self.requests[0].url),
            "https://www.pixiv.net/ajax/novel/28347721?lang=zh",
        )
        self.assertEqual(self.requests[0].headers["referer"], metadata["source_url"])
        self.assertEqual(self.requests[0].headers["cookie"], "PHPSESSID=123_test")

    async def test_series_more_than_thirty_chapters_paginates_and_saves_every_chapter(
        self,
    ):
        def handler(request):
            if "/series_content/" in request.url.path:
                cursor = int(request.url.params["last_order"])
                orders = range(1, 31) if cursor == 0 else ([31] if cursor == 30 else [])
                return catalogue(orders)
            return novel(request.url.path.rsplit("/", 1)[1], series=True)

        self.transport(handler)
        result = await self.downloader.download(Target("series", "15917685"), self.root)
        self.assertEqual(result.folder.name, "系列名称_作者")
        self.assertEqual(result.chapters, 31)
        self.assertEqual(len(list(result.folder.glob("第*.txt"))), 31)
        self.assertTrue((result.folder / "第31章 章节 31.txt").is_file())
        requests = [r for r in self.requests if "/series_content/" in r.url.path]
        self.assertEqual(
            [r.url.params["last_order"] for r in requests], ["0", "30", "31"]
        )
        self.assertEqual(requests[0].url.params["limit"], "30")
        self.assertEqual(requests[0].url.params["order_by"], "asc")
        self.assertEqual(
            requests[0].headers["referer"],
            "https://www.pixiv.net/novel/series/15917685",
        )
        metadata = json.loads(
            (result.folder / "metadata.json").read_text(encoding="utf-8")
        )
        self.assertEqual([c["order"] for c in metadata["chapters"]], list(range(1, 32)))
        self.assertEqual(metadata["saved_chapters"], metadata["total_chapters"])

    async def test_ordering_gaps_and_duplicate_boundary_entries(self):
        def handler(request):
            if "/series_content/" in request.url.path:
                cursor = int(request.url.params["last_order"])
                return catalogue([3, 1] if cursor == 0 else [3, 5], last=cursor > 0)
            return novel(request.url.path.rsplit("/", 1)[1], series=True)

        self.transport(handler)
        result = await self.downloader.download(Target("series", "15917685"), self.root)
        metadata = json.loads(
            (result.folder / "metadata.json").read_text(encoding="utf-8")
        )
        self.assertEqual([c["order"] for c in metadata["chapters"]], [1, 3, 5])
        self.assertTrue((result.folder / "第5章 章节 5.txt").is_file())

    async def test_failed_chapter_keeps_partial_files_and_failed_metadata(self):
        def handler(request):
            if "/series_content/" in request.url.path:
                return catalogue([1, 2], last=True)
            data = novel(request.url.path.rsplit("/", 1)[1], series=True)
            if data["id"] == "2":
                del data["content"]
            return data

        self.transport(handler)
        with self.assertRaises(DownloadError) as raised:
            await self.downloader.download(Target("series", "15917685"), self.root)
        self.assertIn("已保存 1/2", str(raised.exception))
        folder = next(self.root.iterdir())
        metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["status"], "failed")
        self.assertEqual(metadata["saved_chapters"], 1)
        self.assertTrue((folder / "第1章 章节 1.txt").is_file())

    async def test_repeated_download_does_not_overwrite_existing_files(self):
        data = novel("1", title="作品")
        self.transport(lambda request: data)
        first = await self.downloader.download(Target("novel", "1"), self.root)
        data["content"] = "更新正文"
        second = await self.downloader.download(Target("novel", "1"), self.root)
        self.assertNotEqual(first.folder, second.folder)
        self.assertEqual(first.folder.name, "作品_作者")
        self.assertNotEqual(
            (first.folder / "第1章 作品.txt").read_text(encoding="utf-8"),
            data["content"],
        )
        self.assertEqual(
            (second.folder / "第1章 作品.txt").read_text(encoding="utf-8"),
            data["content"],
        )

    async def test_chapter_write_failure_records_failed_metadata(self):
        self.transport(lambda request: novel("1"))
        from astrbot_plugin_pixiv_novel_sub import downloader

        original = downloader.write_text

        def write(path, content):
            if path.name.startswith("第"):
                raise OSError("磁盘已满")
            original(path, content)

        with (
            patch.object(downloader, "write_text", side_effect=write),
            self.assertRaises(DownloadError) as raised,
        ):
            await self.downloader.download(Target("novel", "1"), self.root)
        self.assertIn("已保存 0/1", str(raised.exception))
        metadata = json.loads(
            next(self.root.glob("*/metadata.json")).read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["status"], "failed")
        self.assertEqual(metadata["saved_chapters"], 0)

    async def test_mismatched_novel_id_not_saved(self):
        self.transport(lambda request: novel("999"))
        with self.assertRaises(DownloadError):
            await self.downloader.download(Target("novel", "1"), self.root)
        self.assertFalse(self.root.exists())

    async def test_title_and_author_cannot_escape_save_directory(self):
        data = novel("1", title="../../越界")
        data["userName"] = "../作者"
        self.transport(lambda request: data)
        result = await self.downloader.download(Target("novel", "1"), self.root)
        self.assertEqual(result.folder.parent, self.root.resolve())
        self.assertEqual(len(list(result.folder.glob("第*.txt"))), 1)

    async def test_nonadvancing_pagination_stops(self):
        self.transport(lambda request: catalogue([1]))
        with self.assertRaises(DownloadError) as raised:
            await self.downloader.download(Target("series", "15917685"), self.root)
        self.assertIn("分页未前进", str(raised.exception))
        self.assertEqual(len(self.requests), 2)

    async def test_empty_and_unavailable_series_not_reported_complete(self):
        for data in (catalogue([]), catalogue([1], last=True)):
            if data["page"]["seriesContents"]:
                data["page"]["seriesContents"][0]["available"] = False
            self.client.get_json = AsyncMock(
                return_value={"error": False, "body": data}
            )
            with self.assertRaises(DownloadError):
                await self.downloader.download(Target("series", "15917685"), self.root)
        self.assertFalse(self.root.exists())

    async def test_missing_title_in_navigation_uses_thumbnail_series_title(self):
        def handler(request):
            if "/series_content/" in request.url.path:
                return catalogue([1], last=True, title="目录系列名")
            return novel("1")

        self.transport(handler)
        result = await self.downloader.download(Target("series", "15917685"), self.root)
        self.assertEqual(result.folder.name, "目录系列名_作者")

    async def test_missing_series_title_fetches_series_metadata(self):
        def handler(request):
            if "/series_content/" in request.url.path:
                return catalogue([1], last=True)
            if "/series/" in request.url.path:
                return {"title": "系列信息名", "caption": "系列简介"}
            return novel("1")

        self.transport(handler)
        result = await self.downloader.download(Target("series", "15917685"), self.root)
        self.assertEqual(result.folder.name, "系列信息名_作者")
        metadata = json.loads(
            (result.folder / "metadata.json").read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["description"], "系列简介")

    async def test_cancelled_download_records_partial_status(self):
        started = asyncio.Event()

        async def fetch_novel(key):
            if key == "1":
                return novel("1", series=True)
            started.set()
            await asyncio.Event().wait()

        self.downloader.catalogue = AsyncMock(
            return_value=([{"id": "1", "order": 1}, {"id": "2", "order": 2}], {})
        )
        self.downloader.novel = fetch_novel
        task = asyncio.create_task(
            self.downloader.download(Target("series", "15917685"), self.root)
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        metadata = json.loads(
            next(self.root.glob("*/metadata.json")).read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["status"], "cancelled")
        self.assertEqual(metadata["saved_chapters"], 1)


if __name__ == "__main__":
    unittest.main()
