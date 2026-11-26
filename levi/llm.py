"""LLM backends with a single interface, a hard call budget, and a response cache.

Token discipline (why Levi is cheap to run on a Codex subscription):
  1. Deterministic collectors run first and cost zero tokens.
  2. Every LLM call is structured-output only (JSON Schema) - no rambling prose.
  3. Prompts carry compacted evidence (stripped text, capped length), never raw HTML.
  4. Two tiers: 'fast' (low reasoning effort) for extraction/classification,
     'deep' only for discovery and final memos on the shortlist.
  5. Content-addressed cache: the same question is never paid for twice within TTL.
  6. A per-run call budget (LEVI_MAX_LLM_CALLS) that fails closed.
  7. Codex runs in an empty scratch dir so it does not ingest repo files / AGENTS.md
     as context on every call.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from .config import Settings
from .db import DB


class BudgetExceeded(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


def strict_schema(schema: dict) -> dict:
    """Make a JSON Schema acceptable to OpenAI/Codex strict structured outputs:
    every object closes additionalProperties and lists every property as required.
    Optional-ness is expressed with nullable types instead."""
    s = json.loads(json.dumps(schema))

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" or (isinstance(node.get("type"), list) and "object" in node["type"]):
                props = node.setdefault("properties", {})
                node["additionalProperties"] = False
                node["required"] = list(props.keys())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(s)
    return s


def extract_json(text: str):
    """Best-effort JSON extraction from a model reply (for backends without schema enforcement)."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    for open_, close in (("{", "}"), ("[", "]")):
        i, j = text.find(open_), text.rfind(close)
        if i != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"model did not return JSON: {text[:300]}")


class LLM:
    def __init__(self, settings: Settings, db: DB):
        self.s = settings
        self.db = db
        self.backend = settings.resolved_backend()
        self.calls = 0

    # ------------------------------------------------------------------ public
    def ask(self, prompt: str, schema: dict, *, purpose: str, tier: str = "fast",
            web: bool = False, system: str | None = None, cache_days: float | None = None):
        if self.backend == "none":
            raise LLMError("No LLM backend. Install & log in to Codex (`codex login`), "
                           "or set OPENAI_API_KEY / ANTHROPIC_API_KEY.")
        model = self.s.deep_model if tier == "deep" else self.s.fast_model
        key = DB.key(self.backend, model, tier, web, system, prompt, schema)
        ttl = self.s.llm_cache_days if cache_days is None else cache_days
        if ttl > 0:
            hit = self.db.cache_get(key, ttl)
            if hit is not None:
                self.db.log_usage(self.backend, purpose, True, len(prompt), 0, 0.0)
                return hit
        if self.calls >= self.s.max_llm_calls:
            raise BudgetExceeded(f"LLM call budget of {self.s.max_llm_calls} reached "
                                 "(raise with --max-llm-calls or LEVI_MAX_LLM_CALLS)")
        self.calls += 1
        t0 = time.time()
        fn = {"codex": self._codex, "openai": self._openai, "anthropic": self._anthropic}[self.backend]
        out = fn(prompt, schema, tier=tier, web=web, system=system, model=model)
        self.db.log_usage(self.backend, purpose, False, len(prompt), len(json.dumps(out)), time.time() - t0)
        if ttl > 0:
            self.db.cache_put(key, out)
        return out

    # ------------------------------------------------------------------ codex
    def codex_cmd(self, schema_path: str, out_path: str, *, tier: str, web: bool, model: str) -> list[str]:
        effort = "medium" if tier == "deep" else "low"
        cmd = [
            "codex", "exec",
            "--skip-git-repo-check",
            "--sandbox", "read-only",
            "--output-schema", schema_path,
            "-o", out_path,
            "-c", f'model_reasoning_effort="{effort}"',
            "-c", f'web_search="{"live" if web else "disabled"}"',
        ]
        if model:
            cmd += ["-m", model]
        cmd.append("-")  # prompt from stdin
        return cmd

    def _codex(self, prompt, schema, *, tier, web, system, model):
        if not shutil.which("codex"):
            raise LLMError("codex CLI not found on PATH")
        with tempfile.TemporaryDirectory(prefix="levi-codex-") as td:
            sp, op = Path(td, "schema.json"), Path(td, "out.json")
            sp.write_text(json.dumps(strict_schema(schema)))
            full = (system + "\n\n" if system else "") + prompt + \
                "\n\nDo not run shell commands or edit files. Answer only with the JSON object."
            proc = subprocess.run(self.codex_cmd(str(sp), str(op), tier=tier, web=web, model=model),
                                  input=full, text=True, capture_output=True, cwd=td,
                                  timeout=900 if tier == "deep" else 300)
            if proc.returncode != 0 or not op.exists():
                raise LLMError(f"codex exec failed ({proc.returncode}): {proc.stderr[-800:]}")
            return extract_json(op.read_text())

    # ------------------------------------------------------------------ openai (BYO key)
    def _openai(self, prompt, schema, *, tier, web, system, model):
        model = model or ("gpt-5" if tier == "deep" else "gpt-5-mini")
        body = {
            "model": model,
            "input": ([{"role": "system", "content": system}] if system else []) +
                     [{"role": "user", "content": prompt}],
            "reasoning": {"effort": "medium" if tier == "deep" else "low"},
            "text": {"format": {"type": "json_schema", "name": "levi", "strict": True,
                                "schema": strict_schema(schema)}},
        }
        if web:
            body["tools"] = [{"type": "web_search"}]
        req = urllib.request.Request(
            "https://api.openai.com/v1/responses", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            data = json.loads(r.read())
        texts = [c.get("text", "") for item in data.get("output", []) if item.get("type") == "message"
                 for c in item.get("content", []) if c.get("type") == "output_text"]
        return extract_json("".join(texts))

    # ------------------------------------------------------------------ anthropic (BYO key)
    def _anthropic(self, prompt, schema, *, tier, web, system, model):
        try:
            import anthropic  # optional dependency: pip install anthropic
        except ImportError as e:
            raise LLMError("pip install anthropic to use the anthropic backend") from e
        client = anthropic.Anthropic()
        model = model or ("claude-opus-5-5" if tier == "deep" else "claude-haiku-5-5")
        kwargs = dict(model=model, max_tokens=16000,
                      output_config={"effort": "medium" if tier == "deep" else "low"})
        if system:
            kwargs["system"] = system
        if web:
            # web results carry citations, which can't be combined with a forced JSON format,
            # so we ask for JSON in the prompt and parse it.
            wtype = "web_search_20260209" if "haiku" not in model else "web_search_20250305"
            kwargs["tools"] = [{"type": wtype, "name": "web_search", "max_uses": 8}]
            prompt += "\n\nReturn ONLY JSON matching this schema:\n" + json.dumps(schema)
        else:
            kwargs["output_config"]["format"] = {"type": "json_schema", "schema": strict_schema(schema)}
        messages = [{"role": "user", "content": prompt}]
        for _ in range(4):  # server tools may pause long turns
            resp = client.messages.create(messages=messages, **kwargs)
            if resp.stop_reason == "refusal":
                raise LLMError("model refused the request")
            if resp.stop_reason != "pause_turn":
                break
            messages = [messages[0], {"role": "assistant", "content": resp.content}]
        text = "".join(b.text for b in resp.content if b.type == "text")
        return extract_json(text)
