from __future__ import annotations

import asyncio
import json
import os
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .monitor import PixivClient, PixivError, now

BASE = "https://www.pixiv.net"


class DownloadError(Exception):
    """A download failed without exposing credentials or response contents."""


@dataclass(frozen=True)
class Target:
    kind: str
    id: str

    @property
    def url(self) -> str:
        if self.kind == "series":
            return f"{BASE}/novel/series/{self.id}"
        return f"{BASE}/novel/show.php?id={self.id}"

    @classmethod
    def parse(cls, url: str) -> Target:
        try:
            parsed = urlsplit(url.strip())
            if parsed.scheme != "https" or parsed.netloc.lower() not in {
                "pixiv.net",
                "www.pixiv.net",
            }:
                raise ValueError
            match = re.fullmatch(r"/novel/series/([1-9][0-9]*)/?", parsed.path)
            if match:
                return cls("series", match.group(1))
            ids = parse_qs(parsed.query).get("id", [])
            if (
                parsed.path == "/novel/show.php"
                and len(ids) == 1
                and re.fullmatch(r"[1-9][0-9]*", ids[0])
            ):
                return cls("novel", ids[0])
        except ValueError:
            pass
        raise DownloadError(
            "请提供 Pixiv 小说或系列的完整 HTTPS 链接：/novel/show.php?id=编号 或 /novel/series/编号。"
        )


def safe_name(text: str, limit: int = 80) -> str:
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = " ".join(text.split()).strip(" .")[:limit].rstrip(" .") or "未命名"
    if re.fullmatch(
        r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", text, re.IGNORECASE
    ):
        text = "_" + text
    return text


def write_text(path: Path, content: str):
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def write_metadata(folder: Path, metadata: dict):
    write_text(
        folder / "metadata.json",
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
    )


def field(body: dict, key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DownloadError(f"Pixiv 小说缺少必要字段 {key}，无法完整保存。")
    return value


def validate_novel(body: dict, expected_id: str) -> dict:
    if "id" in body and str(body["id"]) != expected_id:
        raise DownloadError("Pixiv 返回的小说 ID 与请求不一致。")
    field(body, "title")
    field(body, "userName")
    if not isinstance(body.get("content"), str):
        raise DownloadError(
            "Pixiv 未返回小说正文，作品可能已删除或当前账号无访问权限。"
        )
    return body


@dataclass
class DownloadResult:
    folder: Path
    title: str
    author: str
    chapters: int


class NovelDownloader:
    def __init__(self, client: PixivClient):
        self.client = client

    async def body(self, path: str, params: dict, referer: str) -> dict:
        # Downloads are sequential; a small fixed interval avoids a burst of
        # requests for long series without adding more user-facing settings.
        await asyncio.sleep(0.5)
        return (await self.client.get_json(path, params, referer))["body"]

    async def novel(self, key: str) -> dict:
        body = await self.body(
            f"/ajax/novel/{key}", {"lang": "zh"}, Target("novel", key).url
        )
        return validate_novel(body, key)

    async def catalogue(self, target: Target) -> tuple[list[dict], dict]:
        cursor, entries, seen_ids, seen_orders = 0, [], set(), set()
        first_body = {}
        for _ in range(1000):
            body = await self.body(
                f"/ajax/novel/series_content/{target.id}",
                {"limit": 30, "last_order": cursor, "order_by": "asc", "lang": "zh"},
                target.url,
            )
            if not first_body:
                first_body = body
            page = body.get("page")
            items = page.get("seriesContents") if isinstance(page, dict) else None
            if not isinstance(items, list):
                raise DownloadError("Pixiv 系列响应缺少 page.seriesContents 列表。")
            if not items:
                if page.get("isLastPage") is False:
                    raise DownloadError(
                        "Pixiv 系列目录不完整：尚未到末页但返回了空列表。"
                    )
                break
            next_cursor = cursor
            for item in items:
                if not isinstance(item, dict):
                    raise DownloadError("Pixiv 系列章节格式异常。")
                key = str(item.get("id", ""))
                series = item.get("series", {})
                order = series.get("contentOrder") if isinstance(series, dict) else None
                if order is None:
                    order = item.get("order")
                if isinstance(order, str) and re.fullmatch(r"[1-9][0-9]*", order):
                    order = int(order)
                if (
                    not re.fullmatch(r"[1-9][0-9]*", key)
                    or type(order) is not int
                    or order <= 0
                ):
                    raise DownloadError(
                        "系列中存在无法识别的章节 ID 或顺序，无法完整下载。"
                    )
                next_cursor = max(next_cursor, order)
                if key in seen_ids:
                    continue
                if order <= cursor or order in seen_orders:
                    raise DownloadError("Pixiv 系列章节顺序冲突，无法完整下载。")
                if item.get("available") is False or item.get("isMasked") is True:
                    raise DownloadError(f"系列第 {order} 章不可访问，无法完整下载。")
                seen_ids.add(key)
                seen_orders.add(order)
                entries.append(
                    {"id": key, "order": order, "title": str(item.get("title") or "")}
                )
            if next_cursor <= cursor:
                raise DownloadError("Pixiv 系列分页未前进，已停止以避免重复请求。")
            cursor = next_cursor
            if page.get("isLastPage") is True:
                break
        else:
            raise DownloadError("系列分页超过 1000 页，无法确认目录是否完整。")
        if not entries:
            raise DownloadError("系列没有可下载的章节。")
        return sorted(entries, key=lambda item: item["order"]), first_body

    def create_folder(
        self, root: Path, title: str, author: str, target: Target
    ) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        # Allow room for a chapter filename on Windows with a conventional path
        # length limit. Metadata retains complete untruncated titles.
        limit = min(80, 230 - len(str(root)) - 70)
        if limit < 12:
            raise DownloadError("保存根目录过长，请在配置中使用更短的路径。")
        base = safe_name(f"{title}_{author}", limit)
        for index in range(10000):
            suffix = (
                ""
                if index == 0
                else f"_{target.id}" + (f"_{index}" if index > 1 else "")
            )
            folder = root / (base + suffix)
            try:
                folder.mkdir()
                return folder
            except FileExistsError:
                continue
        raise DownloadError("同名保存目录过多，请整理保存目录后重试。")

    async def download(
        self,
        target: Target,
        root: Path,
        progress: Callable[[str, int, int, str], None] | None = None,
    ) -> DownloadResult:
        def report(stage: str, done: int, total: int, title: str = ""):
            if progress:
                progress(stage, done, total, title)

        report("获取章节", 0, 0)
        if target.kind == "series":
            entries, first_page = await self.catalogue(target)
            first = await self.novel(entries[0]["id"])
            navigation = first.get("seriesNavData") or {}
            title = navigation.get("title") if isinstance(navigation, dict) else None
            if not isinstance(title, str) or not title.strip():
                thumbnails = first_page.get("thumbnails", {}).get("novel", [])
                title = next(
                    (
                        item.get("seriesTitle")
                        for item in thumbnails
                        if isinstance(item, dict)
                        and str(item.get("id")) == entries[0]["id"]
                    ),
                    None,
                )
            series_info = {}
            if not isinstance(title, str) or not title.strip():
                series_info = await self.body(
                    f"/ajax/novel/series/{target.id}", {"lang": "zh"}, target.url
                )
                title = field(series_info, "title")
            author = first["userName"]
            description = series_info.get("caption", "")
        else:
            first = await self.novel(target.id)
            entries = [{"id": target.id, "order": 1, "title": first["title"]}]
            title, author = first["title"], first["userName"]
            description = first.get("description", "")

        root = root.expanduser().resolve()
        folder = self.create_folder(root, title, author, target)
        metadata = {
            "type": target.kind,
            "id": target.id,
            "title": title,
            "author": author,
            "author_id": first.get("userId", ""),
            "source_url": target.url,
            "description": description,
            "saved_at": now(),
            "status": "downloading",
            "total_chapters": len(entries),
            "saved_chapters": 0,
            "chapters": [],
        }
        try:
            write_metadata(folder, metadata)
            for index, entry in enumerate(entries):
                report("下载章节", index, len(entries), title)
                novel = first if index == 0 else await self.novel(entry["id"])
                prefix = f"第{entry['order']}章 "
                limit = min(80, 230 - len(str(folder)) - len(prefix) - 4)
                if limit < 8:
                    raise DownloadError("保存目录过长，无法创建章节文件。")
                filename = prefix + safe_name(novel["title"], limit) + ".txt"
                # Preserve Pixiv's complete content and markup; text is never
                # interpreted as HTML, template code or bot instructions.
                write_text(folder / filename, novel["content"].replace("\r\n", "\n"))
                metadata["chapters"].append(
                    {
                        "id": entry["id"],
                        "order": entry["order"],
                        "title": novel["title"],
                        "author": novel["userName"],
                        "filename": filename,
                        "source_url": Target("novel", entry["id"]).url,
                        "create_date": novel.get("createDate", ""),
                        "upload_date": novel.get("uploadDate", ""),
                        "description": novel.get("description", ""),
                        "tags": novel.get("tags", {}),
                    }
                )
                metadata["saved_chapters"] = index + 1
                write_metadata(folder, metadata)
                report("下载章节", index + 1, len(entries), title)
            metadata.update(status="complete", completed_at=now())
            write_metadata(folder, metadata)
        except asyncio.CancelledError:
            self.record_failure(
                folder, metadata, "cancelled", "插件卸载或重载，下载中止。"
            )
            raise
        except (PixivError, DownloadError, OSError, UnicodeError) as exc:
            reason = (
                str(exc)
                if isinstance(exc, (PixivError, DownloadError))
                else "写入文件失败，请检查目录权限和磁盘空间。"
            )
            self.record_failure(folder, metadata, "failed", reason)
            raise DownloadError(
                f"下载未完成，已保存 {metadata['saved_chapters']}/{len(entries)} 章：{reason}"
            ) from None
        report("已完成", len(entries), len(entries), title)
        return DownloadResult(folder, title, author, len(entries))

    @staticmethod
    def record_failure(folder: Path, metadata: dict, status: str, reason: str):
        metadata.update(status=status, error=reason)
        try:
            write_metadata(folder, metadata)
        except (OSError, UnicodeError):
            pass
