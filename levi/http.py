"""Tiny cached HTTP client (stdlib only)."""
from __future__ import annotations

import gzip
import json
import re
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from html import unescape

from .config import Settings
from .db import DB

_retrievals: ContextVar[list[float] | None] = ContextVar("http_retrievals", default=None)


class Http:
    def __init__(self, settings: Settings, db: DB):
        self.s = settings
        self.db = db

    @contextmanager
    def capture_retrievals(self):
        times = []
        token = _retrievals.set(times)
        try:
            yield times
        finally:
            _retrievals.reset(token)

    def get(self, url: str, *, headers: dict | None = None, data: bytes | None = None,
            cache_days: float | None = None, timeout: int = 20) -> tuple[int, str]:
        ttl = self.s.http_cache_days if cache_days is None else cache_days
        key = DB.key("http", url, headers, data)
        if ttl > 0:
            hit = self.db.cache_get(key, ttl, with_timestamp=True)
            if hit is not None:
                (status, text), retrieved_at = hit
                if (times := _retrievals.get()) is not None:
                    times.append(retrieved_at)
                return status, text
        h = {"User-Agent": self.s.user_agent, "Accept-Encoding": "gzip"}
        h.update(headers or {})
        req = urllib.request.Request(url, headers=h, data=data)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                status, text = r.status, body.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status, text = e.code, ""
        except Exception:  # network errors are data, not crashes
            return 0, ""
        if status == 200:
            retrieved_at = time.time()
            if (times := _retrievals.get()) is not None:
                times.append(retrieved_at)
            if ttl > 0:
                self.db.cache_put(key, [status, text], ts=retrieved_at)
        return status, text

    def json(self, url: str, **kw):
        status, text = self.get(url, **kw)
        if status != 200 or not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None


def html_to_text(html: str, limit: int = 6000) -> str:
    """Compact visible text - what we send to an LLM instead of raw HTML (10-30x fewer tokens)."""
    html = re.sub(r"(?is)<(script|style|noscript|svg|head).*?</\1>", " ", html)
    text = unescape(re.sub(r"(?s)<[^>]+>", " ", html))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]
