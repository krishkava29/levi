"""SQLite store: companies, time-series signal snapshots, scores, and caches.

Snapshots are the secret weapon. Every enrichment writes a dated row, so the second
time Levi looks at a company it can compute *velocity* (reviews/month, follower
growth, new roles) instead of static levels. Velocity is what finds companies early.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  website TEXT,
  city TEXT, state TEXT, country TEXT DEFAULT 'US',
  category TEXT, model TEXT, thesis TEXT,
  ownership TEXT,
  profile JSON DEFAULT '{}',
  status TEXT DEFAULT 'new',          -- new | watching | contacted | passed | pipeline
  first_seen REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS snapshots (
  company_id INTEGER, signal TEXT, value REAL, raw JSON, source TEXT, ts REAL,
  PRIMARY KEY (company_id, signal, ts)
);
CREATE TABLE IF NOT EXISTS scores (
  company_id INTEGER, ts REAL, score REAL, alpha REAL, coverage REAL, card JSON,
  PRIMARY KEY (company_id, ts)
);
CREATE TABLE IF NOT EXISTS cache (
  key TEXT PRIMARY KEY, value BLOB, ts REAL
);
CREATE TABLE IF NOT EXISTS usage (
  ts REAL, backend TEXT, purpose TEXT, cached INTEGER, in_chars INTEGER, out_chars INTEGER, seconds REAL
);
"""


class _Rows(list):
    def fetchone(self):
        return self[0] if self else None


class _Locked:
    """Serialise all access to a sqlite3 connection across threads."""

    def __init__(self, conn):
        object.__setattr__(self, "_c", conn)
        object.__setattr__(self, "_l", threading.RLock())

    def __getattr__(self, name):
        attr = getattr(self._c, name)
        if not callable(attr):
            return attr

        def call(*a, **kw):
            with self._l:
                res = attr(*a, **kw)
                if isinstance(res, sqlite3.Cursor) and name == "execute" and \
                        str(a[0]).lstrip().upper().startswith("SELECT"):
                    return _Rows(res.fetchall())  # materialise inside the lock
                return res
        return call

    def __setattr__(self, name, value):
        setattr(self._c, name, value)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # collectors run in threads; one connection guarded by a lock is plenty for this write volume
        self.conn = _Locked(sqlite3.connect(str(path), check_same_thread=False, timeout=30))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------ cache
    @staticmethod
    def key(*parts) -> str:
        return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()

    def cache_get(self, key: str, max_age_days: float):
        row = self.conn.execute("SELECT value, ts FROM cache WHERE key=?", (key,)).fetchone()
        if row and time.time() - row["ts"] < max_age_days * 86400:
            return json.loads(row["value"])
        return None

    def cache_put(self, key: str, value) -> None:
        self.conn.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, json.dumps(value), time.time()))
        self.conn.commit()

    def log_usage(self, backend, purpose, cached, in_chars, out_chars, seconds) -> None:
        self.conn.execute("INSERT INTO usage VALUES (?,?,?,?,?,?,?)",
                          (time.time(), backend, purpose, int(cached), in_chars, out_chars, seconds))
        self.conn.commit()

    def usage_summary(self) -> list[dict]:
        q = """SELECT backend, purpose, COUNT(*) calls, SUM(cached) cache_hits,
                      SUM(in_chars) in_chars, SUM(out_chars) out_chars, ROUND(SUM(seconds),1) seconds
               FROM usage GROUP BY backend, purpose ORDER BY calls DESC"""
        return [dict(r) for r in self.conn.execute(q)]

    # ------------------------------------------------------------ companies
    def upsert_company(self, c: dict) -> int:
        slug = slugify(c["name"])
        now = time.time()
        row = self.conn.execute("SELECT id, profile FROM companies WHERE slug=?", (slug,)).fetchone()
        cols = ["website", "city", "state", "country", "category", "model", "thesis", "ownership"]
        if row:
            prof = json.loads(row["profile"] or "{}")
            prof.update(c.get("profile") or {})
            sets = ", ".join(f"{k}=COALESCE(?, {k})" for k in cols)
            self.conn.execute(f"UPDATE companies SET {sets}, profile=?, updated=? WHERE id=?",
                              [c.get(k) for k in cols] + [json.dumps(prof), now, row["id"]])
            cid = row["id"]
        else:
            cur = self.conn.execute(
                f"INSERT INTO companies (slug,name,{','.join(cols)},profile,first_seen,updated) "
                f"VALUES (?,?,{','.join('?' * len(cols))},?,?,?)",
                [slug, c["name"]] + [c.get(k) for k in cols] + [json.dumps(c.get("profile") or {}), now, now])
            cid = cur.lastrowid
        self.conn.commit()
        return cid

    def company(self, ident) -> sqlite3.Row | None:
        if isinstance(ident, int) or str(ident).isdigit():
            return self.conn.execute("SELECT * FROM companies WHERE id=?", (int(ident),)).fetchone()
        return self.conn.execute("SELECT * FROM companies WHERE slug=? OR name=? COLLATE NOCASE",
                                 (slugify(str(ident)), ident)).fetchone()

    def companies(self, thesis: str | None = None) -> list[sqlite3.Row]:
        if thesis:
            return list(self.conn.execute("SELECT * FROM companies WHERE thesis=?", (thesis,)))
        return list(self.conn.execute("SELECT * FROM companies"))

    def set_status(self, cid: int, status: str) -> None:
        self.conn.execute("UPDATE companies SET status=?, updated=? WHERE id=?", (status, time.time(), cid))
        self.conn.commit()

    # ------------------------------------------------------------ signals
    def snap(self, cid: int, signal: str, value, raw=None, source: str = "") -> None:
        v = None
        if isinstance(value, bool):
            v = 1.0 if value else 0.0
        elif isinstance(value, (int, float)):
            v = float(value)
        self.conn.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?)",
                          (cid, signal, v, json.dumps(raw if raw is not None else value, default=str),
                           source, time.time()))
        self.conn.commit()

    def latest_signals(self, cid: int) -> dict:
        q = """SELECT s.signal, s.value, s.raw FROM snapshots s
               JOIN (SELECT signal, MAX(ts) ts FROM snapshots WHERE company_id=? GROUP BY signal) m
               ON s.signal=m.signal AND s.ts=m.ts WHERE s.company_id=?"""
        out = {}
        for r in self.conn.execute(q, (cid, cid)):
            out[r["signal"]] = r["value"] if r["value"] is not None else json.loads(r["raw"])
        return out

    def history(self, cid: int, signal: str) -> list[tuple[float, float]]:
        return [(r["ts"], r["value"]) for r in self.conn.execute(
            "SELECT ts, value FROM snapshots WHERE company_id=? AND signal=? AND value IS NOT NULL ORDER BY ts",
            (cid, signal))]

    def save_score(self, cid: int, card) -> None:
        self.conn.execute("INSERT OR REPLACE INTO scores VALUES (?,?,?,?,?,?)",
                          (cid, time.time(), card.score, card.alpha, card.coverage,
                           json.dumps({"pillars": card.pillars, "reasons": card.top_reasons(),
                                       "concerns": card.concerns()})))
        self.conn.commit()

    def leaderboard(self, thesis: str | None = None, by: str = "alpha", limit: int = 50) -> list[dict]:
        by = "alpha" if by not in ("alpha", "score") else by
        q = f"""SELECT c.*, s.score, s.alpha, s.coverage, s.card FROM companies c
                JOIN scores s ON s.company_id=c.id
                JOIN (SELECT company_id, MAX(ts) ts FROM scores GROUP BY company_id) m
                  ON m.company_id=s.company_id AND m.ts=s.ts
                {'WHERE c.thesis=?' if thesis else ''}
                ORDER BY s.{by} DESC LIMIT ?"""
        args = (thesis, limit) if thesis else (limit,)
        return [dict(r) for r in self.conn.execute(q, args)]
