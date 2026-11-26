"""Runtime configuration. Everything is overridable by env var or CLI flag.

Backends (in default preference order):
  codex      - the user's own Codex CLI login (ChatGPT subscription). Zero marginal $.
  openai     - OPENAI_API_KEY (BYO key)
  anthropic  - ANTHROPIC_API_KEY (BYO key)
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


def _home() -> Path:
    return Path(os.environ.get("LEVI_HOME", Path.home() / ".levi"))


@dataclass
class Settings:
    home: Path = field(default_factory=_home)
    backend: str = field(default_factory=lambda: os.environ.get("LEVI_BACKEND", "auto"))
    # Two model tiers: "fast" for extraction/classification, "deep" for discovery & memos.
    # Empty = let the backend pick its own default (Codex uses whatever the user's plan defaults to).
    fast_model: str = field(default_factory=lambda: os.environ.get("LEVI_FAST_MODEL", ""))
    deep_model: str = field(default_factory=lambda: os.environ.get("LEVI_DEEP_MODEL", ""))
    max_llm_calls: int = field(default_factory=lambda: int(os.environ.get("LEVI_MAX_LLM_CALLS", "60")))
    llm_cache_days: int = field(default_factory=lambda: int(os.environ.get("LEVI_LLM_CACHE_DAYS", "14")))
    http_cache_days: int = field(default_factory=lambda: int(os.environ.get("LEVI_HTTP_CACHE_DAYS", "3")))
    google_places_key: str | None = field(default_factory=lambda: os.environ.get("GOOGLE_PLACES_API_KEY"))
    serpapi_key: str | None = field(default_factory=lambda: os.environ.get("SERPAPI_API_KEY"))
    user_agent: str = "LeviSourcingBot/0.1 (+research; respects robots.txt)"

    @property
    def db_path(self) -> Path:
        return self.home / "levi.db"

    def resolved_backend(self) -> str:
        if self.backend != "auto":
            return self.backend
        if shutil.which("codex"):
            return "codex"
        if os.environ.get("OPENAI_API_KEY"):
            return "openai"
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "anthropic"
        return "none"
