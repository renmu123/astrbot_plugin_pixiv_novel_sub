from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ENDPOINT = "https://www.pixiv.net/ajax/follow_latest/novel"
REFERER = "https://www.pixiv.net/novel/bookmark_new.php"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:157.0) Gecko/20100101 Firefox/157.0"
)


class PixivError(Exception):
    """A safe error message that never includes request headers or response text."""


class StateError(Exception):
    """Persistent state could not be read or written safely."""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def novel_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise PixivError("Pixiv 返回了无效的小说 ID，未更新上次结果。")
    result = str(value)
    if not re.fullmatch(r"[1-9][0-9]*", result):
        raise PixivError("Pixiv 返回了无效的小说 ID，未更新上次结果。")
    return result


def clean_text(value: Any, limit: int = 200) -> str:
    return " ".join(str(value or "").split())[:limit]


def parse_response(payload: Any) -> list[dict]:
    """Validate a successful feed, excluding thumbnails outside page.ids."""
    if not isinstance(payload, dict) or payload.get("error") is not False:
        raise PixivError("Pixiv API 返回错误，请检查 Cookie 登录状态；未更新上次结果。")
    body = payload.get("body")
    if not isinstance(body, dict):
        raise PixivError("Pixiv 响应缺少 body，未更新上次结果。")
    thumbnails = body.get("thumbnails")
    entries = thumbnails.get("novel") if isinstance(thumbnails, dict) else None
    if not isinstance(entries, list):
        raise PixivError("Pixiv 响应缺少 thumbnails.novel 列表，未更新上次结果。")
    by_id = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise PixivError("Pixiv 小说列表格式异常，未更新上次结果。")
        key = novel_id(entry.get("id"))
        by_id[key] = {
            "id": key,
            "title": clean_text(entry.get("title")) or f"小说 {key}",
            "user_name": clean_text(entry.get("userName")) or "未知作者",
            "create_date": clean_text(entry.get("createDate"), 50),
        }
    page = body.get("page", {})
    if not isinstance(page, dict):
        raise PixivError("Pixiv 分页数据格式异常，未更新上次结果。")
    if "ids" in page:
        if not isinstance(page["ids"], list):
            raise PixivError("Pixiv 分页 ID 列表格式异常，未更新上次结果。")
        ids = [novel_id(value) for value in page["ids"]]
        if any(key not in by_id for key in ids):
            raise PixivError("Pixiv 小说列表不完整，未更新上次结果。")
        return [by_id[key] for key in dict.fromkeys(ids)]
    return list(by_id.values())


@dataclass(frozen=True)
class Options:
    cookie: str = ""
    proxy_mode: str = "system"
    proxy_url: str = ""

    def __post_init__(self):
        if self.proxy_mode not in {"system", "custom", "none"}:
            raise ValueError("代理模式必须为 system、custom 或 none。")
        if self.proxy_mode == "custom" and not self.proxy_url:
            raise ValueError("自定义代理模式需要填写代理地址。")
        if "\r" in self.cookie or "\n" in self.cookie:
            raise ValueError("Cookie 不能包含换行。")

    @property
    def cookie_account(self) -> str:
        match = re.search(r"(?:^|;\s*)PHPSESSID=([0-9]+)_", self.cookie)
        return match.group(1) if match else ""

    @property
    def account_key(self) -> str:
        # Session renewal for the same account retains its baseline. Unknown
        # accounts use a digest; neither the Cookie nor its token is persisted.
        identity = (
            self.cookie_account or hashlib.sha256(self.cookie.encode()).hexdigest()
        )
        return hashlib.sha256(f"{identity}:all:1:zh".encode()).hexdigest()


class PixivClient:
    def __init__(self, options: Options):
        self.options = options
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30,
                trust_env=self.options.proxy_mode == "system",
                proxy=self.options.proxy_url
                if self.options.proxy_mode == "custom"
                else None,
                follow_redirects=False,
                # Only advertise encodings supported without optional codecs.
                headers={"Accept-Encoding": "gzip, deflate"},
            )
        return self._client

    async def get_json(self, path: str, params: dict, referer: str) -> dict:
        """Request an internal Pixiv API using the configured login and proxy."""
        if not self.options.cookie:
            raise PixivError("请先在插件配置中填写 Pixiv Cookie，然后重新加载插件。")
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9,zh-TW;q=0.8,zh-HK;q=0.7,en-US;q=0.6,en;q=0.5",
            "Referer": referer,
            "Cookie": self.options.cookie,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        if self.options.cookie_account:
            headers["x-user-id"] = self.options.cookie_account
        for attempt in range(3):
            try:
                response = await self._get_client().get(
                    "https://www.pixiv.net" + path,
                    params=params,
                    headers=headers,
                )
            except httpx.HTTPError:
                if attempt < 2:
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
                raise PixivError("请求 Pixiv 失败，请检查网络和代理。") from None
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
                raise PixivError(
                    f"Pixiv 暂时不可用（HTTP {response.status_code}），稍后重试。"
                )
            if response.status_code in {401, 403} or 300 <= response.status_code < 400:
                raise PixivError(
                    "Pixiv 拒绝访问或要求登录，请更新 Cookie；如有验证请先在浏览器完成。"
                )
            if response.status_code != 200:
                raise PixivError(f"Pixiv 请求失败（HTTP {response.status_code}）。")
            try:
                payload = response.json()
            except (ValueError, UnicodeError):
                raise PixivError(
                    "Pixiv 未返回有效 JSON，可能需要登录或浏览器验证。"
                ) from None
            if not isinstance(payload, dict) or payload.get("error") is not False:
                raise PixivError("Pixiv API 返回错误，请检查 Cookie 和作品访问权限。")
            if not isinstance(payload.get("body"), dict):
                raise PixivError("Pixiv 响应缺少 body 或格式异常。")
            return payload
        raise PixivError("Pixiv 请求失败。")

    async def fetch(self) -> tuple[dict, list[dict]]:
        payload = await self.get_json(
            "/ajax/follow_latest/novel", {"p": 1, "mode": "all", "lang": "zh"}, REFERER
        )
        return payload, parse_response(payload)

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def empty_state() -> dict:
    return {
        "version": 2,
        "account_key": "",
        "initialized": False,
        "seen_ids": [],
        "latest_ids": [],
        "last_success": "",
        "response": None,
    }


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        if not self.path.exists():
            return empty_state()
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("version") not in {1, 2}:
                raise ValueError
            if state["version"] == 1:
                # Preserve the old comparison baseline while discarding the
                # removed notification/session state. No messages are sent.
                responses = state["responses"]
                response = responses[0]["response"] if responses else None
                state = {
                    key: state[key]
                    for key in (
                        "account_key",
                        "initialized",
                        "seen_ids",
                        "latest_ids",
                        "last_success",
                    )
                }
                state.update(version=2, response=response)
            for key in ("seen_ids", "latest_ids"):
                if not isinstance(state[key], list):
                    raise TypeError
            for key in ("seen_ids", "latest_ids"):
                if any(not isinstance(value, str) for value in state[key]):
                    raise ValueError
            if type(state["initialized"]) is not bool:
                raise ValueError
            if any(
                not isinstance(state[key], str)
                for key in ("account_key", "last_success")
            ):
                raise ValueError
            for key in ("seen_ids", "latest_ids"):
                for value in state[key]:
                    novel_id(value)
            if state["last_success"]:
                datetime.fromisoformat(state["last_success"])
            if state["initialized"]:
                parse_response(state["response"])
            return state
        except (OSError, ValueError, TypeError, IndexError, KeyError, PixivError):
            raise StateError(
                "状态文件无法读取或格式损坏，已保留原文件；请检查或恢复 state.json。"
            ) from None

    def save(self, state: dict):
        temp = self.path.with_suffix(".json.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temp.open("w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
        except (OSError, ValueError, TypeError):
            raise StateError(
                "无法保存状态文件，未推进比较基线；请检查磁盘空间和目录权限。"
            ) from None


def format_novels(novels: list[dict]) -> str:
    lines = [f"Pixiv 关注小说更新（{len(novels)} 篇）"]
    for item in novels:
        lines.append(f"\n{item['title']}\n作者：{item['user_name']}")
        if item.get("create_date"):
            lines.append(f"发布时间：{item['create_date']}")
        lines.append(f"https://www.pixiv.net/novel/show.php?id={item['id']}")
    return "\n".join(lines)


@dataclass
class CheckResult:
    baseline: bool
    count: int
    new: list[dict]


class Watcher:
    def __init__(self, client: PixivClient, store: StateStore):
        self.client, self.store = client, store
        self.state = store.load()
        self.lock = asyncio.Lock()

    def commit(self, candidate: dict):
        # Memory changes only after atomic persistence, so a failed write cannot
        # silently consume an update.
        self.store.save(candidate)
        self.state = candidate

    async def check(self) -> CheckResult:
        async with self.lock:
            key = self.client.options.account_key
            same_account = self.state["account_key"] == key
            response, novels = await self.client.fetch()
            candidate = copy.deepcopy(self.state)
            baseline = not candidate["initialized"] or not same_account
            seen = set(candidate["seen_ids"]) if not baseline else set()
            previous = set(candidate["latest_ids"]) if not baseline else set()
            new = (
                [
                    item
                    for item in novels
                    if item["id"] not in previous and item["id"] not in seen
                ]
                if not baseline
                else []
            )
            if baseline:
                candidate["seen_ids"] = []
            for item in novels:
                if item["id"] not in seen:
                    candidate["seen_ids"].append(item["id"])
                    seen.add(item["id"])
            candidate.update(
                account_key=key,
                initialized=True,
                last_success=now(),
                latest_ids=[item["id"] for item in novels],
                response=response,
            )
            self.commit(candidate)
            return CheckResult(baseline, len(novels), new)
