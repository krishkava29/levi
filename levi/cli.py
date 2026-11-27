"""levi - momentum-first PE sourcing agent.

  levi doctor                         check backend (Codex login / API keys) and optional data keys
  levi theses                         list built-in theses
  levi signals                        print the signal taxonomy
  levi run <thesis|"free text">       full funnel: discover -> AI probe -> collect -> score -> enrich -> memo
  levi discover <thesis>              LLM+web discovery only
  levi import <file.json> [--thesis]  import researched targets
  levi collect [--thesis T]           free (zero-token) signal collection
  levi enrich <company>               deep LLM+web research on one company
  levi score [--thesis T]             recompute scores (zero tokens)
  levi top [--thesis T] [--by alpha]  leaderboard
  levi show <company> [--md]          full dossier
  levi evidence <company>            claims, citations, and missing evidence
  levi memo <company>                challenge and screening memo (LLM)
  levi export --csv FILE              export leaderboard
  levi watch [--thesis T]             re-collect signals and report movers (run on a schedule)
  levi status <company> <state>       pipeline state: new|watching|contacted|passed|pipeline
  levi usage                          LLM calls, cache hits, chars in/out
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

from .config import Settings
from .llm import BudgetExceeded, LLMError
from .pipeline import Levi, list_theses, load_thesis
from .report import dossier_md, evidence_md, leaderboard_rows, table, to_csv
from .signals import PILLARS, by_pillar


def _levi(a) -> Levi:
    s = Settings()
    if getattr(a, "backend", None):
        s.backend = a.backend
    if getattr(a, "max_llm_calls", None):
        s.max_llm_calls = a.max_llm_calls
    return Levi(s, log=lambda m: print(m, file=sys.stderr))


def _company(levi: Levi, ident: str):
    row = levi.db.company(ident)
    if not row:
        sys.exit(f"unknown company: {ident}")
    return row


def cmd_doctor(a):
    s = Settings()
    print(f"LEVI_HOME        {s.home}")
    print(f"backend          {s.resolved_backend()}  (LEVI_BACKEND={s.backend})")
    print(f"codex on PATH    {'yes' if shutil.which('codex') else 'no  -> npm i -g @openai/codex && codex login'}")
    print(f"GOOGLE_PLACES    {'set' if s.google_places_key else 'not set (review rating/count signals off)'}")
    print(f"SERPAPI          {'set' if s.serpapi_key else 'not set (Google rank + Trends signals off)'}")
    print(f"LLM call budget  {s.max_llm_calls} per run")


def cmd_signals(a):
    for p, sigs in by_pillar().items():
        print(f"\n## {PILLARS[p]}")
        for s in sigs:
            print(f"  [{s.tier:4}] {s.name:<42} w={s.weight:<4} {s.why}")


def cmd_run(a):
    levi = _levi(a)
    rows = levi.run(a.thesis, discover=not a.no_discover, enrich_top=a.enrich_top, memo_top=a.memo_top,
                    per_angle=a.per_angle)
    print(table(leaderboard_rows(levi, load_thesis(a.thesis)["name"], limit=a.limit)))
    print(f"\n{levi.llm.calls} LLM calls this run (budget {levi.s.max_llm_calls}).", file=sys.stderr)


def cmd_discover(a):
    levi = _levi(a)
    ids = levi.discover(load_thesis(a.thesis), per_angle=a.per_angle)
    print(f"{len(ids)} new companies")


def cmd_import(a):
    levi = _levi(a)
    n = 0
    for f in a.files:
        n += levi.import_seeds(f, a.thesis)
    for r in levi.db.companies():
        levi.score_company(r["id"])
    print(f"imported {n} companies")


def cmd_collect(a):
    levi = _levi(a)
    rows = levi.db.companies(a.thesis)
    for r in rows:
        found = levi.collect_free(r["id"])
        print(f"{r['name']:<36} {len(found)} signals", file=sys.stderr)
        levi.score_company(r["id"])


def cmd_enrich(a):
    levi = _levi(a)
    row = _company(levi, a.company)
    out = levi.enrich(row["id"])
    levi.score_company(row["id"])
    print(json.dumps(out, indent=2))


def cmd_score(a):
    levi = _levi(a)
    for r in levi.db.companies(a.thesis):
        levi.score_company(r["id"])
    print(table(leaderboard_rows(levi, a.thesis, by=a.by, limit=a.limit)))


def cmd_top(a):
    levi = _levi(a)
    rows = leaderboard_rows(levi, a.thesis, by=a.by, limit=a.limit)
    print(json.dumps(rows, indent=2, default=str) if a.json else table(rows))


def cmd_show(a):
    levi = _levi(a)
    d = levi.dossier(_company(levi, a.company)["id"])
    print(dossier_md(d) if a.md else json.dumps(d, indent=2, default=str))


def cmd_evidence(a):
    levi = _levi(a)
    row = _company(levi, a.company)
    cutoff = datetime.combine(a.as_of, time.max, timezone.utc).timestamp() if a.as_of else None
    evidence = levi.evidence(row["id"], as_of=cutoff)
    print(json.dumps(evidence, indent=2) if a.json else evidence_md(evidence))


def cmd_memo(a):
    levi = _levi(a)
    cid = _company(levi, a.company)["id"]
    try:
        levi.memo(cid)
    except (LLMError, BudgetExceeded) as error:
        sys.exit(str(error))
    print(dossier_md(levi.dossier(cid)))


def cmd_export(a):
    levi = _levi(a)
    rows = leaderboard_rows(levi, a.thesis, by=a.by, limit=10_000)
    Path(a.csv).write_text(to_csv(rows))
    print(f"wrote {len(rows)} rows -> {a.csv}")


def cmd_watch(a):
    """Re-collect zero-token signals; print companies whose alpha moved. Schedule with cron."""
    levi = _levi(a)
    before = {r["id"]: r["alpha"] for r in levi.db.leaderboard(a.thesis, limit=10_000)}
    for r in levi.db.companies(a.thesis):
        if r["status"] == "passed":
            continue
        levi.collect_free(r["id"])
        levi.score_company(r["id"])
    after = levi.db.leaderboard(a.thesis, limit=10_000)
    movers = sorted(((r["alpha"] - before.get(r["id"], r["alpha"]), r) for r in after),
                    key=lambda x: abs(x[0]), reverse=True)
    for delta, r in movers[:15]:
        if abs(delta) >= a.min_delta:
            print(f"{'UP  ' if delta > 0 else 'DOWN'} {delta:+5.1f}  {r['name']}  "
                  f"(Alpha {r['alpha']}, experimental)")


def cmd_status(a):
    levi = _levi(a)
    levi.db.set_status(_company(levi, a.company)["id"], a.state)
    print("ok")


def cmd_usage(a):
    levi = _levi(a)
    for r in levi.db.usage_summary():
        print(r)


def main(argv=None):
    p = argparse.ArgumentParser(prog="levi", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--backend", choices=["auto", "codex", "openai", "anthropic"])
    p.add_argument("--max-llm-calls", type=int)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *args):
        sp = sub.add_parser(name)
        sp.set_defaults(fn=fn)
        for spec in args:
            flags, kw = spec
            sp.add_argument(*flags, **kw)
        return sp

    T = ([" --thesis".strip()], {"default": None})
    BY = (["--by"], {"default": "alpha", "choices": ["alpha", "score"]})
    LIM = (["--limit"], {"type": int, "default": 25})
    add("doctor", cmd_doctor)
    add("theses", lambda a: print("\n".join(list_theses())))
    add("signals", cmd_signals)
    add("run", cmd_run, (["thesis"], {}), (["--no-discover"], {"action": "store_true"}),
        (["--enrich-top"], {"type": int, "default": 10}), (["--memo-top"], {"type": int, "default": 3}),
        (["--per-angle"], {"type": int, "default": 8}), LIM)
    add("discover", cmd_discover, (["thesis"], {}), (["--per-angle"], {"type": int, "default": 8}))
    add("import", cmd_import, (["files"], {"nargs": "+"}), T)
    add("collect", cmd_collect, T)
    add("enrich", cmd_enrich, (["company"], {}))
    add("score", cmd_score, T, BY, LIM)
    add("top", cmd_top, T, BY, LIM, (["--json"], {"action": "store_true"}))
    add("show", cmd_show, (["company"], {}), (["--md"], {"action": "store_true"}))
    add("evidence", cmd_evidence, (["company"], {}), (["--json"], {"action": "store_true"}),
        (["--as-of"], {"type": date.fromisoformat, "metavar": "YYYY-MM-DD",
                      "help": "Use only evidence available by the end of this UTC date"}))
    add("memo", cmd_memo, (["company"], {}))
    add("export", cmd_export, (["--csv"], {"required": True}), T, BY)
    add("watch", cmd_watch, T, (["--min-delta"], {"type": float, "default": 2.0}))
    add("status", cmd_status, (["company"], {}),
        (["state"], {"choices": ["new", "watching", "contacted", "passed", "pipeline"]}))
    add("usage", cmd_usage)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
