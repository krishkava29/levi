"""The Levi funnel. Cheap signals for everyone, expensive tokens for the few.

   discover (LLM+web, per thesis angle)       ~6 calls / thesis
 + ai_probe (LLM, no web, per thesis prompt)  ~4 calls / thesis -> scores ALL companies at once
 -> collect_free (HTTP only)                   0 tokens, every company
 -> score                                      0 tokens
 -> enrich (LLM+web) top-K only                1 call / shortlisted company
 -> financials triangulation                   0 tokens
 -> score again, memo (LLM) top-N only         1 call / finalist
"""
from __future__ import annotations

import json
import re
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import collectors as C
from . import prompts as P
from .config import Settings
from .db import DB, slugify
from .financials import triangulate
from .http import Http
from .llm import LLM, BudgetExceeded, LLMError
from .scoring import bayesian_rating, revenue_fit, score

THESIS_DIR = Path(__file__).parent / "theses"


def load_thesis(name_or_path: str) -> dict:
    p = Path(name_or_path)
    if not p.exists():
        p = THESIS_DIR / f"{name_or_path.replace('-', '_')}.toml"
    if not p.exists():
        # free-text thesis: let the user just type what they want
        return {"name": slugify(name_or_path)[:40] or "adhoc", "description": name_or_path,
                "geo": "United States", "revenue_band": [5e6, 100e6], "model": "default",
                "keywords": [], "ai_prompts": [f"What are the best {name_or_path}?"], "pillar_weights": {}}
    with open(p, "rb") as f:
        t = tomllib.load(f)
    t.setdefault("pillar_weights", {})
    return t


def list_theses() -> list[str]:
    return sorted(p.stem.replace("_", "-") for p in THESIS_DIR.glob("*.toml"))


class Levi:
    def __init__(self, settings: Settings | None = None, log=print):
        self.s = settings or Settings()
        self.db = DB(self.s.db_path)
        self.http = Http(self.s, self.db)
        self.llm = LLM(self.s, self.db)
        self.log = log

    # ------------------------------------------------------------------ discover
    def discover(self, thesis: dict, per_angle: int = 8, angles: list[str] | None = None) -> list[int]:
        known = [r["name"] for r in self.db.companies()]
        ids = []
        for angle in angles or P.DISCOVERY_ANGLES:
            self.log(f"  discover [{angle.split(':')[0]}]")
            try:
                out = self.llm.ask(P.discovery_prompt(thesis, angle, known, per_angle), P.DISCOVERY_SCHEMA,
                                   purpose="discover", tier="deep", web=True, system=P.SYSTEM)
            except (LLMError, BudgetExceeded) as e:
                self.log(f"    ! {e}")
                break
            for c in out.get("companies", []):
                if not c.get("name") or c["name"] in known:
                    continue
                known.append(c["name"])
                ids.append(self.db.upsert_company({
                    "name": c["name"], "website": c.get("website"), "city": c.get("city"), "state": c.get("state"),
                    "category": c.get("category"), "thesis": thesis["name"], "model": thesis.get("model"),
                    "ownership": c.get("ownership_guess"),
                    "profile": {"discovery_why": c.get("why"), "evidence": c.get("evidence_urls", [])}}))
        return ids

    def import_seeds(self, path: str, thesis_name: str | None = None) -> int:
        """Import researched targets (e.g. data/seed_targets.json) incl. their sourced facts."""
        rows = json.loads(Path(path).read_text())
        n = 0
        for r in rows:
            cid = self.db.upsert_company({
                "name": r["name"], "website": r.get("website"), "city": r.get("hq_city"), "state": r.get("hq_state"),
                "category": r.get("category"), "model": r.get("business_model"),
                "thesis": thesis_name or r.get("thesis"), "ownership": r.get("ownership"), "profile": r})
            self._apply_facts(cid, r, source="seed")
            n += 1
        return n

    # ------------------------------------------------------------------ AI visibility (once per thesis)
    def ai_probe(self, thesis: dict) -> dict:
        answers: list[list[str]] = []
        for p in P.ai_probe_prompts(thesis):
            try:
                out = self.llm.ask(p, P.AI_PROBE_SCHEMA, purpose="ai_probe", tier="fast", web=False, cache_days=7)
                answers.append(out.get("recommendations", []))
            except (LLMError, BudgetExceeded) as e:
                self.log(f"    ! {e}")
                break
        if not answers:
            return {}
        result = {}
        for row in self.db.companies(thesis["name"]):
            hits, ranks = 0, []
            pat = re.compile(r"\b" + re.escape(row["name"].lower()) + r"\b")
            for ans in answers:
                for i, rec in enumerate(ans):
                    if pat.search(rec.lower()):
                        hits += 1
                        ranks.append(i + 1)
                        break
            sov = hits / len(answers)
            self.db.snap(row["id"], "ai_share_of_voice", sov, source=self.llm.backend)
            if ranks:
                self.db.snap(row["id"], "ai_rank", sum(ranks) / len(ranks), source=self.llm.backend)
            result[row["name"]] = sov
        # names the AI recommends that we don't track yet are free discovery leads
        known = {r["name"].lower() for r in self.db.companies()}
        leads = sorted({n for a in answers for n in a if n.lower() not in known})
        self.db.cache_put(DB.key("ai_leads", thesis["name"]), leads)
        return {"share_of_voice": result, "untracked_ai_recommendations": leads}

    # ------------------------------------------------------------------ free collectors
    def collect_free(self, cid: int) -> dict:
        row = self.db.company(cid)
        name, site = row["name"], row["website"]
        found: dict = {}

        def run(fn, *a):
            try:
                return fn(*a)
            except Exception as e:  # a broken collector must never kill a run
                return {}, {"error": f"{fn.__name__}: {e}"}

        with ThreadPoolExecutor(max_workers=6) as ex:
            fut = {
                "website": ex.submit(run, C.website, self.http, site) if site else None,
                "wayback": ex.submit(run, C.wayback, self.http, site) if site else None,
                "hn": ex.submit(run, C.hackernews, self.http, name),
                "reddit": ex.submit(run, C.reddit, self.http, name),
                "wiki": ex.submit(run, C.wikipedia, self.http, name),
                "places": ex.submit(run, C.google_places, self.http, self.s.google_places_key, name, row["city"]),
                "trends": ex.submit(run, C.trends, self.http, self.s.serpapi_key, name),
            }
            results = {k: f.result() for k, f in fut.items() if f}
        raw_all = {k: v[1] for k, v in results.items()}
        for k, (vals, _) in results.items():
            found.update(vals)
        ats = raw_all.get("website", {}).get("ats")
        if ats:
            vals, raw = run(C.jobs, self.http, tuple(ats))
            found.update(vals)
            raw_all["jobs"] = raw
        counts = [raw_all.get("hn", {}).get("hn_hits"), raw_all.get("reddit", {}).get("reddit_posts_12m")]
        if any(c is not None for c in counts):  # only when a source actually answered
            found["community_mentions"] = sum(c or 0 for c in counts)
        thesis = self._thesis_for(row)
        if self.s.serpapi_key and thesis.get("keywords"):
            vals, raw = run(C.serp_rank, self.http, self.s.serpapi_key, C.domain_of(site), thesis["keywords"])
            found.update(vals)
            raw_all["serp"] = raw
        for sid, v in found.items():
            self.db.snap(cid, sid, v, source="free")
        self.db.snap(cid, "_raw_free", None, raw={k: {kk: vv for kk, vv in v.items() if kk != "text"}
                                                  for k, v in raw_all.items()}, source="free")
        prof = {"socials": raw_all.get("website", {}).get("socials"),
                "first_archived": raw_all.get("wayback", {}).get("first_archived"),
                "site_text": raw_all.get("website", {}).get("text")}
        self.db.upsert_company({"name": row["name"], "profile": {k: v for k, v in prof.items() if v}})
        self._velocity(cid)
        return found

    def _velocity(self, cid: int) -> None:
        """Turn repeated snapshots into growth signals (the reason to keep watching)."""
        for level, growth, per_month in (("social_followers", "social_growth_90d", False),
                                         ("review_count", "review_velocity", True),
                                         ("open_roles", None, False)):
            h = self.db.history(cid, level)
            if len(h) < 2:
                continue
            (t0, v0), (t1, v1) = h[0], h[-1]
            days = (t1 - t0) / 86400
            if days < 7 or not v0:
                continue
            if growth and not per_month:
                self.db.snap(cid, growth, (v1 / v0 - 1) * (90 / days), source="derived")
            elif growth:
                self.db.snap(cid, growth, (v1 - v0) / days * 30, source="derived")

    # ------------------------------------------------------------------ LLM enrichment (shortlist)
    def enrich(self, cid: int) -> dict:
        row = self.db.company(cid)
        have = self.db.latest_signals(cid)
        ctx = json.dumps({k: v for k, v in have.items() if not k.startswith("_")}, default=str)
        out = self.llm.ask(P.enrich_prompt(row["name"], row["website"], ctx), P.ENRICH_SCHEMA,
                           purpose="enrich", tier="deep", web=True, system=P.SYSTEM)
        self.db.upsert_company({"name": row["name"], "website": out.get("website"), "city": out.get("hq_city"),
                                "state": out.get("hq_state"), "category": out.get("category"),
                                "model": out.get("business_model"), "ownership": out.get("ownership"),
                                "profile": {"enrichment": out}})
        self._apply_facts(cid, out, source="llm")
        return out

    def _apply_facts(self, cid: int, f: dict, source: str) -> None:
        """Map a researched fact sheet (enrichment or seed) onto signals."""
        def g(*keys):
            for k in keys:
                cur = f
                for part in k.split("."):
                    cur = cur.get(part) if isinstance(cur, dict) else None
                if cur not in (None, "", []) and not isinstance(cur, dict):
                    return cur
            return None

        own = (g("ownership") or "").lower()
        if own:
            founder = any(w in own for w in ("founder", "family", "bootstrap", "self-funded"))
            institutional = any(w in own for w in ("pe_backed", "vc_backed", "private equity", "strategic", "public",
                                                   "acquired", "vc", "venture", "growth-backed", "series"))
            if institutional or founder:  # ambiguous ownership stays unknown rather than penalised
                self.db.snap(cid, "founder_owned", founder and not institutional, source=source)
        mapping = {
            "capital_raised": g("capital_raised_usd"),
            "employees": g("employees", "employees.value"),
            "new_locations_12m": g("new_locations_or_doors_12m"),
            "social_followers": g("social_followers_total"),
            "press_velocity": g("press_mentions_6m"),
            "awards": g("has_growth_award"),
            "institutional_awareness": g("institutional_awareness"),
            "succession_signal": g("succession_signal"),
            "fragmented_market": g("fragmented_market"),
            "product_talkability": g("product_talkability"),
            "recurring_model": g("recurring_model"),
            "category_tailwind": g("category_tailwind"),
            "pricing_power": g("pricing_power"),
            "fad_risk": g("fad_risk"),
            "legal_regulatory": g("legal_regulatory"),
            "key_person": g("key_person"),
            "revenue_growth": g("growth_rate_estimate"),
        }
        if not mapping["social_followers"]:
            soc = f.get("social") or {}
            tot = sum(v for k, v in soc.items() if k.endswith("followers") and isinstance(v, (int, float)))
            mapping["social_followers"] = tot or None
        rating = g("review_rating", "reviews.rating")
        count = g("review_count", "reviews.count")
        if rating:
            mapping["review_rating"] = bayesian_rating(float(rating), float(count or 0))
        if count:
            mapping["review_count"] = count
        for sid, v in mapping.items():
            if v is not None and sid != "employees":
                self.db.snap(cid, sid, v, source=source)
        # financial facts are stored raw for triangulation
        facts = {
            "reported_revenue": g("revenue_reported_usd") or (g("revenue.value_usd") if (g("revenue.type") or "") in
                                                              ("reported",) else None),
            "reported_source": g("revenue_source_url", "revenue.source_url"),
            "inc_band": g("inc5000_revenue_band_usd"),
            "press_estimate": g("third_party_revenue_estimate_usd") or (
                g("revenue.value_usd") if (g("revenue.type") or "") not in ("reported", "") else None),
            "press_source": g("third_party_source_url", "revenue.source_url"),
            "system_sales": g("system_sales_usd") or (g("revenue.value_usd") if g("revenue.type") == "system-sales" else None),
            "units": g("units_or_locations", "units.count", "locations.count"),
            "auv": g("auv_usd", "revenue.auv_usd"),
            "employees": g("employees", "employees.value"),
            "review_count": count,
            "growth": g("growth_rate_estimate"),
        }
        if facts["system_sales"] and facts["press_estimate"] == facts["system_sales"]:
            facts["press_estimate"] = None
        facts = {k: v for k, v in facts.items() if v not in (None, "", [])}
        for k in ("units", "employees", "review_count"):
            if isinstance(facts.get(k), str):
                m = re.search(r"[\d,]+", facts[k])
                facts[k] = float(m.group(0).replace(",", "")) if m else None
        if isinstance(facts.get("inc_band"), list) and len(facts["inc_band"]) != 2:
            facts.pop("inc_band")
        old = self.db.latest_signals(cid).get("_facts") or {}
        old.update({k: v for k, v in facts.items() if v is not None})
        self.db.snap(cid, "_facts", None, raw=old, source=source)

    # ------------------------------------------------------------------ financials + scoring
    def financials(self, cid: int) -> dict:
        row = self.db.company(cid)
        facts = self.db.latest_signals(cid).get("_facts") or {}
        view = triangulate(facts, row["model"])
        self.db.snap(cid, "_financials", None, raw=view.as_dict(), source="levi")
        return view.as_dict()

    def _thesis_for(self, row) -> dict:
        try:
            return load_thesis(row["thesis"]) if row["thesis"] else load_thesis("adhoc")
        except Exception:
            return load_thesis("adhoc")

    def score_company(self, cid: int):
        row = self.db.company(cid)
        thesis = self._thesis_for(row)
        fin = self.financials(cid)
        vals = {k: v for k, v in self.db.latest_signals(cid).items() if not k.startswith("_")}
        facts = self.db.latest_signals(cid).get("_facts") or {}
        if fin.get("mid"):
            band = tuple(thesis.get("revenue_band", (5e6, 100e6)))
            vals["revenue_fit"] = revenue_fit(fin["mid"], facts.get("growth"), band)
            vals["revenue_confidence"] = fin["confidence"]
            vals["margin_profile"] = fin["indicative_pnl"]["ebitda_margin"] / 0.35
        if "founder_owned" in vals:
            vals["founder_owned"] = bool(vals["founder_owned"])
        card = score(vals, thesis.get("pillar_weights"))
        self.db.save_score(cid, card)
        return card

    # ------------------------------------------------------------------ memo
    def memo(self, cid: int) -> dict:
        d = self.dossier(cid)
        out = self.llm.ask(P.memo_prompt(json.dumps(d, default=str)), P.MEMO_SCHEMA,
                           purpose="memo", tier="deep", web=False, system=P.SYSTEM)
        self.db.snap(cid, "_memo", None, raw=out, source=self.llm.backend)
        return out

    def dossier(self, cid: int) -> dict:
        row = self.db.company(cid)
        sig = self.db.latest_signals(cid)
        prof = json.loads(row["profile"] or "{}")
        prof.pop("site_text", None)
        lb = [r for r in self.db.leaderboard(limit=100000) if r["id"] == cid]
        return {
            "company": {k: row[k] for k in ("name", "website", "city", "state", "category", "model", "ownership",
                                            "thesis", "status")},
            "score": ({"levi_score": lb[0]["score"], "levi_alpha": lb[0]["alpha"], "coverage": lb[0]["coverage"],
                       **json.loads(lb[0]["card"])} if lb else None),
            "signals": {k: v for k, v in sig.items() if not k.startswith("_")},
            "financials": sig.get("_financials"),
            "profile": prof,
            "memo": sig.get("_memo"),
        }

    # ------------------------------------------------------------------ the whole funnel
    def run(self, thesis_name: str, discover: bool = True, enrich_top: int = 10, memo_top: int = 3,
            per_angle: int = 8) -> list[dict]:
        thesis = load_thesis(thesis_name)
        self.log(f"[levi] thesis={thesis['name']} backend={self.llm.backend} budget={self.s.max_llm_calls} calls")
        if discover:
            self.log("[1/6] discovery (LLM + web)")
            self.discover(thesis, per_angle=per_angle)
        rows = self.db.companies(thesis["name"])
        self.log(f"[2/6] AI share-of-voice probe across {len(rows)} companies")
        self.ai_probe(thesis)
        self.log("[3/6] free signal collection (0 tokens)")
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(lambda r: self.collect_free(r["id"]), rows))
        self.log("[4/6] screening score")
        for r in rows:
            self.score_company(r["id"])
        shortlist = self.db.leaderboard(thesis["name"], by="score", limit=enrich_top)
        self.log(f"[5/6] deep enrichment for top {len(shortlist)}")
        for r in shortlist:
            try:
                self.enrich(r["id"])
            except BudgetExceeded as e:
                self.log(f"    ! {e}")
                break
            except LLMError as e:
                self.log(f"    ! {r['name']}: {e}")
            self.score_company(r["id"])
        final = self.db.leaderboard(thesis["name"], by="alpha", limit=memo_top)
        self.log(f"[6/6] memos for top {len(final)}")
        for r in final:
            try:
                self.memo(r["id"])
            except (LLMError, BudgetExceeded) as e:
                self.log(f"    ! {e}")
                break
        return self.db.leaderboard(thesis["name"], by="alpha")
