"""Outputs: terminal table, Markdown dossiers, CSV export."""
from __future__ import annotations

import csv
import io
import json

from .evidence import iso_time
from .financials import fmt_usd
from .signals import BY_ID, PILLARS


def _loc(r) -> str:
    return ", ".join(x for x in (r.get("city"), r.get("state")) if x) or "n/a"


def leaderboard_rows(levi, thesis: str | None = None, by: str = "alpha", limit: int = 50) -> list[dict]:
    out = []
    for r in levi.db.leaderboard(thesis, by=by, limit=limit):
        sig = levi.db.latest_signals(r["id"])
        card = json.loads(r["card"])
        if "evidence_ids" in card:
            fin = levi.db.snapshot(r["id"], "_financials", r["score_ts"]) or {}
            facts = fin
        else:
            fin = sig.get("_financials") or {}
            facts = sig.get("_facts") or {}
        prof = json.loads(r["profile"] or "{}")
        out.append({
            "id": r["id"], "name": r["name"], "location": _loc(r), "category": r["category"], "thesis": r["thesis"],
            "ownership": r["ownership"], "website": r["website"],
            "levi_score": r["score"], "levi_alpha": r["alpha"], "alpha_experimental": True,
            "alpha_breakdown": card.get("alpha_breakdown"), "coverage": r["coverage"],
            "rev_low": fin.get("low"), "rev_mid": fin.get("mid"), "rev_high": fin.get("high"),
            "rev_confidence": fin.get("confidence"),
            "rev_basis": ", ".join(m["method"] for m in fin.get("methods", [])),
            "system_sales": facts.get("system_sales"),
            "ebitda_est": (fin.get("indicative_pnl") or {}).get("ebitda"),
            "growth": facts.get("growth"),
            "reasons": card.get("reasons", []), "concerns": card.get("concerns", []),
            "pillars": card.get("pillars", {}),
            "pe_angle": prof.get("pe_angle") or (levi.stored_memo(r["id"], sig)[0] or {}).get("thesis"),
            "red_flags": prof.get("red_flags"), "buzz": prof.get("buzz"),
            "sources": prof.get("sources") or prof.get("evidence") or [],
        })
    return out


def table(rows: list[dict]) -> str:
    alpha = "Alpha (experimental)"
    hdr = f"{'#':>3} {'Company':<32} {'Location':<22} {alpha:>20} {'Score':>5} {'Revenue est.':>16} {'Conf':>4}  Why"
    lines = [hdr, "-" * len(hdr)]
    for i, r in enumerate(rows, 1):
        rev = fmt_usd(r["rev_mid"]) if r["rev_mid"] else "n/a"
        if r["rev_low"]:
            rev = f"{fmt_usd(r['rev_low'])}-{fmt_usd(r['rev_high'])}"
        lines.append(f"{i:>3} {r['name'][:32]:<32} {r['location'][:22]:<22} {r['levi_alpha']:>20.1f} "
                     f"{r['levi_score']:>5.1f} {rev:>16} {(r['rev_confidence'] or 0):>4.2f}  "
                     f"{'; '.join(r['reasons'][:2])}")
    return "\n".join(lines)


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    keys = ["name", "location", "category", "thesis", "ownership", "website", "levi_alpha", "alpha_experimental",
            "levi_score", "coverage",
            "rev_low", "rev_mid", "rev_high", "rev_confidence", "rev_basis", "system_sales", "ebitda_est", "growth",
            "reasons", "concerns", "pe_angle", "red_flags", "sources"]
    w = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("; ".join(map(str, r[k])) if isinstance(r.get(k), list) else r.get(k)) for k in keys})
    return buf.getvalue()


def evidence_md(evidence: dict) -> str:
    claims = evidence["claims"]
    counts = ", ".join(f"{kind} {count}" for kind, count in evidence["counts_by_kind"].items())
    coverage = evidence["source_url_coverage"]
    lines = [f"Latest accepted claims by kind: {counts}."]
    if coverage is not None:
        lines += [f"Source URL coverage: {evidence['source_url_count']}/{len(claims)} ({coverage:.0%})."]
    else:
        lines += ["Source URL coverage: no accepted claims."]
    missing = evidence["scored_without_source"]
    lines += ["", "**Scored claims without a source URL**", ""]
    lines += [f"- #{r['id']} {r['claim']} ({r['kind']})" for r in missing] or ["None recorded."]
    if evidence["rejections"]:
        lines += ["", "**Rejected inputs**", ""]
        lines += [f"- {r['signal']} ({r['kind']}): {r['rejection']}" for r in evidence["rejections"]]
    if evidence["validation_issues"]:
        lines += ["", "**Validation findings**", ""]
        lines += [f"- {issue}" for issue in evidence["validation_issues"]]
    all_rows = {r["id"]: r for r in claims + evidence["scored_claims"] + evidence["reviewed_claims"]}
    if all_rows:
        lines += ["", "**Claims and citations**", ""]
    for r in sorted(all_rows.values(), key=lambda r: (r["signal"], -r["id"])):
        citation = f"<{r['source_url']}>" if r["source_url"] else "missing evidence flag"
        value = r["value"]
        if r["signal"] == "revenue_triangulation":
            value = f"{fmt_usd(value['low'])} to {fmt_usd(value['high'])}"
        elif r["signal"] in {"reported_revenue", "press_estimate", "system_sales"}:
            value = f"${value:,}"
        times = (f"period {r['period'] or 'unknown'}, published {r['published_at'] or 'unknown'}, "
                 f"retrieved {iso_time(r['retrieved_at'])}")
        links = f" Inputs: {', '.join(f'#{i}' for i in r['inputs'])}." if r["inputs"] else ""
        lines.append(f"- #{r['id']} **{r['claim']}** ({r['kind']}): {value}. {r['source']}, {times}."
                     f"{links} {r['passage'] or ''} {citation}".strip())
    return "\n".join(lines)


def _objection_lines(challenge: dict) -> list[str]:
    return [f"- **{item['id']} · {item['kind'].replace('_', ' ')}:** {item['concern']}"
            + (f" (claims {', '.join(f'#{i}' for i in item['claim_ids'])})" if item["claim_ids"] else "")
            for item in challenge["objections"]] or ["No objections raised."]


def dossier_md(d: dict) -> str:
    c, s, fin = d["company"], d.get("score") or {}, d.get("financials") or {}
    prof = d.get("profile") or {}
    L = [f"# {c['name']}", "",
         f"**{c.get('category') or ''}** - {', '.join(x for x in (c.get('city'), c.get('state')) if x) or 'location n/a'}"
         f" - {c.get('website') or ''}", "",
         f"**Ownership:** {c.get('ownership') or 'unknown'}", ""]
    if s:
        L += [f"**Levi Alpha (experimental) {s['levi_alpha']} / Levi Score {s['levi_score']}** "
              f"(signal coverage {s['coverage']:.0%})", ""]
        if factors := s.get("alpha_breakdown"):
            L += [f"Fit {factors['fit']:.3f} × institutional-unawareness factor "
                  f"{factors['institutional_unawareness_factor']:.3f} × founder factor "
                  f"{factors['founder_factor']:.3f}.", ""]
        else:
            L += ["Alpha factors are unavailable for this legacy score. Re-run `levi score` to calculate them.", ""]
        L += ["| Pillar | Score |", "|---|---|"]
        L += [f"| {PILLARS[p].split(' - ')[0]} | {v if v is not None else 'n/a'} |" for p, v in s["pillars"].items()]
        L += ["", f"**Strengths:** {', '.join(s.get('reasons') or []) or 'n/a'}",
              f"**Concerns:** {', '.join(s.get('concerns') or []) or 'n/a'}", ""]
    if fin.get("mid"):
        L += ["## Financials (estimated - private company)", "",
              f"Revenue **{fmt_usd(fin['low'])} - {fmt_usd(fin['high'])}** (mid {fmt_usd(fin['mid'])}, "
              f"confidence {fin['confidence']:.0%})", "", "| Method | Range | Confidence | Basis |", "|---|---|---|---|"]
        for m in fin["methods"]:
            src = f" [source]({m['source']})" if m.get("source") else ""
            L.append(f"| {m['method']} | {fmt_usd(m['low'])}-{fmt_usd(m['high'])} | {m['confidence']:.2f} | {m['note']}{src} |")
        p = fin["indicative_pnl"]
        L += ["", f"Indicative P&L ({p['basis']}): revenue {fmt_usd(p['revenue'])}, gross profit {fmt_usd(p['gross_profit'])} "
              f"({p['gross_margin']:.0%}), EBITDA ~{fmt_usd(p['ebitda'])} (range {fmt_usd(p['ebitda_range'][0])}-"
              f"{fmt_usd(p['ebitda_range'][1])})", ""]
    sig = {k: v for k, v in (d.get("signals") or {}).items() if k in BY_ID}
    if sig:
        L += ["## Signals", "", "| Signal | Value |", "|---|---|"]
        L += [f"| {BY_ID[k].name} | {v} |" for k, v in sorted(sig.items())]
        L.append("")
    for key, title in (("growth_evidence", "Growth evidence"), ("buzz", "Why it's hot"), ("pe_angle", "PE angle"),
                       ("red_flags", "Red flags")):
        if prof.get(key):
            L += [f"## {title}", "", str(prof[key]), ""]
    L += ["## Evidence", "", evidence_md(d["evidence"]) if "evidence" in d else "No evidence ledger.", ""]
    L += ["## Challenge", ""]
    if challenge := d.get("challenge"):
        if previous := d.get("memo_challenge"):
            L += ["**Latest challenge awaiting a memo**", "", *_objection_lines(challenge), "",
                  "**Challenge addressed by the saved memo**", "", *_objection_lines(previous)]
        else:
            L += _objection_lines(challenge)
    else:
        L += ["No stored challenge. Running `levi memo` creates one before drafting."]
    L.append("")
    memo = d.get("memo")
    if memo:
        L += ["## Screening memo", "", f"**{memo.get('headline', '')}**", "", memo.get("thesis", ""), ""]
        for k in ("why_now", "value_creation", "key_risks", "diligence_questions"):
            if memo.get(k):
                L += [f"**{k.replace('_', ' ').title()}**", ""] + [f"- {x}" for x in memo[k]] + [""]
        if memo.get("outreach_hook"):
            L += [f"**Outreach hook:** {memo['outreach_hook']}", ""]
        if memo.get("challenge_responses"):
            L += ["**Challenge responses**", ""] + [
                f"- **{item['id']} · {item['disposition']}:** {item['response']}"
                for item in memo["challenge_responses"]
            ] + [""]
    srcs = prof.get("sources") or prof.get("evidence") or []
    if srcs:
        L += ["## Sources", ""] + [f"- {u}" for u in srcs]
    return "\n".join(L)
