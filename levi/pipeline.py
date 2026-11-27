"""The Levi funnel. Cheap signals for everyone, expensive tokens for the few.

   discover (LLM+web, per thesis angle)       ~6 calls / thesis
 + ai_probe (LLM, no web, per thesis prompt)  ~4 calls / thesis -> scores ALL companies at once
 -> collect_free (HTTP only)                   0 tokens, every company
 -> score                                      0 tokens
 -> enrich (LLM+web) top-K only                1 call / shortlisted company
 -> financials triangulation                   0 tokens
 -> score again, challenge + memo (LLM)        2 calls / finalist
"""
from __future__ import annotations

import json
import re
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import collectors as C
from . import prompts as P
from .config import Settings
from .db import DB, slugify
from .evidence import JUDGMENTS, evidence_summary, iso_time, publication_date, signal_error, source_url, valid_number
from .financials import triangulate
from .http import Http
from .llm import LLM, BudgetExceeded, LLMError
from .scoring import bayesian_rating, revenue_fit, score
from .signals import BY_ID

CITED_REVENUE = {"reported_revenue", "press_estimate", "system_sales", "inc_band"}

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
            self.db.record_claim(row["id"], "ai_share_of_voice", sov, kind="measured", source=self.llm.backend,
                                 passage=f"Recommended in {hits} of {len(answers)} model responses.", snapshot=True)
            if ranks:
                self.db.record_claim(row["id"], "ai_rank", sum(ranks) / len(ranks), kind="measured",
                                     source=self.llm.backend, passage=f"Recommendation positions: {ranks}.",
                                     snapshot=True)
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
                with self.http.capture_retrievals() as times:
                    values, raw = fn(*a)
                return values, raw | ({"retrieved_at": max(times)} if times else {})
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
        ats = results.get("website", ({}, {}))[1].get("ats")
        if ats:
            results["jobs"] = run(C.jobs, self.http, tuple(ats))
        thesis = self._thesis_for(row)
        if self.s.serpapi_key and thesis.get("keywords"):
            results["serp"] = run(C.serp_rank, self.http, self.s.serpapi_key, C.domain_of(site), thesis["keywords"])
        raw_all = {k: v[1] for k, v in results.items()}
        community = []
        for collector, (values, raw) in results.items():
            url = raw.get("source_url")
            retrieved_at = raw.get("retrieved_at", time.time())
            observed = dict(values)
            if collector == "website" and url:
                observed.update({k: raw[k] for k in ("socials", "ats", "site_rating", "address", "location_links",
                                                     "has_careers") if raw.get(k) is not None})
            for key in ("first_archived", "hn_hits", "reddit_posts_12m", "places_found", "trend_series"):
                if key in raw and url:
                    observed[key] = raw[key]
            passage = json.dumps({k: v for k, v in raw.items()
                                  if k not in {"source_url", "period", "retrieved_at", "text"}})
            for sid, value in observed.items():
                rejection = signal_error(sid, value) if sid in values else None
                claim_id = self.db.record_claim(cid, sid, value, kind="measured", source=collector, source_url=url,
                                                period=raw.get("period") or iso_time(retrieved_at),
                                                retrieved_at=retrieved_at, passage=passage, snapshot=sid in values,
                                                rejection=rejection)
                if sid in {"hn_hits", "reddit_posts_12m"}:
                    community.append((claim_id, value))
                if sid in values and not rejection:
                    found[sid] = value
        if community:
            found["community_mentions"] = sum(value for _, value in community)
            self.db.record_claim(cid, "community_mentions", found["community_mentions"], kind="measured",
                                 source="community aggregation", inputs=tuple(i for i, _ in community),
                                 passage="Sum of available Hacker News and Reddit measurements.", snapshot=True)
        self.db.snap(cid, "_raw_free", None, raw={k: {kk: vv for kk, vv in v.items() if kk != "text"}
                                                  for k, v in raw_all.items()}, source="free")
        prof = {"socials": raw_all.get("website", {}).get("socials"),
                "first_archived": raw_all.get("wayback", {}).get("first_archived"),
                "site_text": raw_all.get("website", {}).get("text")}
        self.db.upsert_company({"name": row["name"], "profile": {k: v for k, v in prof.items() if v}})
        self._velocity(cid)
        return found

    def evidence(self, cid: int, *, as_of: float | None = None) -> dict:
        row = self.db.latest_score(cid) if as_of is None else None
        return self._evidence(cid, row, as_of=as_of)

    def _evidence(self, cid: int, row, *, as_of: float | None = None) -> dict:
        scorecard = json.loads(row["card"]) if row else {}
        evidence = evidence_summary(self.db.claims(cid, as_of=as_of), scorecard.get("evidence_ids", []))
        evidence["validation_issues"].extend(scorecard.get("validation_issues", []))
        if row and "evidence_ids" not in scorecard:
            evidence["validation_issues"].append("Legacy score has no pinned evidence. Re-run levi score.")
        return evidence

    def _velocity(self, cid: int) -> None:
        """Turn repeated snapshots into growth signals (the reason to keep watching)."""
        for level, growth, days_scaled in (("social_followers", "social_growth_90d", 90),
                                           ("review_count", "review_velocity", 30)):
            history = [r for r in self.db.snapshot_history(cid, level) if r["value"] is not None]
            if len(history) < 2:
                continue
            end = history[-1]
            comparable = [r for r in history if r["source"] == end["source"]]
            start = min(comparable, key=lambda r: r["retrieved_at"] or r["ts"])
            t0, t1 = (r["retrieved_at"] or r["ts"] for r in (start, end))
            v0, v1 = start["value"], end["value"]
            days = (t1 - t0) / 86400
            if days < 7 or (not v0 and days_scaled == 90):
                continue
            change = (v1 / v0 - 1) if days_scaled == 90 else (v1 - v0)
            period = f"{iso_time(t0)}/{iso_time(t1)}"
            inputs = tuple(self._claim_for_snapshot(cid, r) for r in (start, end))
            self.db.record_claim(cid, growth, change * days_scaled / days, kind="measured", source="derived",
                                 period=period, inputs=inputs,
                                 passage=f"{level} changed from {v0:g} to {v1:g}. Scaled to {days_scaled} days.",
                                 snapshot=True)

    # ------------------------------------------------------------------ LLM enrichment (shortlist)
    def enrich(self, cid: int) -> dict:
        row = self.db.company(cid)
        have = self.db.latest_signals(cid)
        ctx = json.dumps({k: v for k, v in have.items() if not k.startswith("_")}, default=str)
        out = self.llm.ask(P.enrich_prompt(row["name"], row["website"], ctx), P.ENRICH_SCHEMA,
                           purpose="enrich", tier="deep", web=True, system=P.SYSTEM)
        if not isinstance(out, dict):
            raise LLMError("Enrichment did not return a fact sheet")
        rejections = self._apply_facts(cid, out, source="llm")
        profile = dict(out)
        if rejections:
            profile["rejected_claims"] = rejections
        self.db.upsert_company({"name": row["name"], "website": out.get("website"), "city": out.get("hq_city"),
                                "state": out.get("hq_state"), "category": out.get("category"),
                                "model": out.get("business_model"), "ownership": out.get("ownership"),
                                "profile": {"enrichment": profile}})
        return profile

    def _apply_facts(self, cid: int, facts: dict, source: str) -> list[dict]:
        """Map a fact sheet onto individually cited observations and scoring inputs."""
        offered = facts.get("evidence")
        evidence = {e["signal"]: e for e in (offered if isinstance(offered, list) else [])
                    if isinstance(e, dict) and isinstance(e.get("signal"), str)}
        rejected = []

        def write(signal, value, kind="reported", *, details=None, snapshot=False, rejection=None):
            if value is None:
                return
            meta = (details or {}) | {k: v for k, v in evidence.get(signal, {}).items() if v not in (None, "")}
            url = source_url(meta.get("source_url"))
            if not rejection and source == "llm" and signal in CITED_REVENUE and not url:
                rejection = "Model-returned revenue requires its own HTTP(S) source URL"
            self.db.record_claim(
                cid, signal, value, kind=kind, source=source, source_url=url,
                passage=meta.get("passage") if isinstance(meta.get("passage"), str) else None,
                period=str(meta["period"]) if isinstance(meta.get("period"), (str, int)) else None,
                published_at=publication_date(meta.get("published_at")), snapshot=snapshot, rejection=rejection)
            if rejection:
                rejected.append({"signal": signal, "value": value, "reason": rejection})
                self.log(f"    ! {signal} rejected: {rejection}")

        def group(key):
            data = facts.get(key)
            return data if isinstance(data, dict) else {}

        def first(*values):
            return next((value for value in values if value is not None), None)

        def metadata(data):
            return {"source_url": data.get("source_url"), "period": first(data.get("period"), data.get("year")),
                    "published_at": data.get("published_at"), "passage": data.get("passage") or data.get("note")}

        own = facts.get("ownership")
        if isinstance(own, str) and own:
            own_text = re.sub(r"\b(no|not|never|without|explicitly no)\s+(outside\s+)?(vc|pe|private equity|venture|"
                              r"institutional|investors?|funding|equity)\b", " ", own.lower())
            founder = any(w in own_text for w in ("founder", "family", "bootstrap", "self-funded"))
            institutional = any(w in own_text for w in ("pe_backed", "vc_backed", "private equity", "strategic",
                                                        "public", "acquired", "vc", "venture", "growth-backed", "series"))
            if institutional or founder:
                write("founder_owned", founder and not institutional, "judgment", snapshot=True,
                      details={"passage": own})

        reviews, social = group("reviews"), group("social")
        rating = first(facts.get("review_rating"), reviews.get("rating"))
        count = first(facts.get("review_count"), reviews.get("count"))
        if isinstance(count, str):
            match = re.search(r"[\d,]+", count)
            count = int(match.group(0).replace(",", "")) if match else count
        if rating is not None:
            error = signal_error("review_rating", rating)
            valid_count = count if count is not None and not signal_error("review_count", count) else None
            write("review_rating", rating if error else bayesian_rating(rating, valid_count), snapshot=True,
                  rejection=error, details=metadata(reviews) | {
                      "passage": f"Raw rating: {rating}. Review count used for Bayesian adjustment: {valid_count}."})
        if count is not None:
            write("review_count", count, snapshot=True, rejection=signal_error("review_count", count),
                  details=metadata(reviews))

        followers = facts.get("social_followers_total")
        if followers is None:
            counts = [v for k, v in social.items() if k.endswith("followers") and valid_number(v)]
            followers = sum(counts) if counts else None
        signals = {
            "capital_raised": facts.get("capital_raised_usd"),
            "new_locations_12m": facts.get("new_locations_or_doors_12m"),
            "social_followers": followers,
            "press_velocity": facts.get("press_mentions_6m"),
            "awards": facts.get("has_growth_award"),
            "recurring_model": facts.get("recurring_model"),
            "revenue_growth": facts.get("growth_rate_estimate"),
        } | {key: facts.get(key) for key in sorted(JUDGMENTS)}
        for signal, value in signals.items():
            if value is None:
                continue
            kind = ("judgment" if signal in JUDGMENTS or signal == "recurring_model"
                    else "estimate" if signal == "revenue_growth" else "reported")
            write(signal, value, kind, snapshot=True, rejection=signal_error(signal, value),
                  details=metadata(social) if signal == "social_followers" else None)

        rev = group("revenue")
        rev_type = rev.get("type")
        for signal, field, url_key, kind, nested in (
            ("reported_revenue", "revenue_reported_usd", "revenue_source_url", "reported", rev_type == "reported"),
            ("press_estimate", "third_party_revenue_estimate_usd", "third_party_source_url", "estimate",
             rev_type not in (None, "", "reported", "system-sales")),
            ("system_sales", "system_sales_usd", "system_sales_source_url", "reported", rev_type == "system-sales"),
            ("inc_band", "inc5000_revenue_band_usd", "inc5000_source_url", "reported", False),
        ):
            value = facts.get(field)
            details = {"source_url": facts.get(url_key), "period": facts.get("revenue_reported_year")
                       if signal == "reported_revenue" else None}
            if value is None and nested:
                value, details = rev.get("value_usd"), metadata(rev)
            if value is None:
                continue
            if signal == "inc_band":
                valid = (isinstance(value, list) and len(value) == 2
                         and all(valid_number(x) for x in value) and 0 < value[0] <= value[1])
            else:
                valid = valid_number(value) and value > 0
            write(signal, value, kind, details=details, rejection=None if valid else "Invalid positive revenue amount or range")

        locations = group("units") or group("locations")
        for signal, value, details in (
            ("units", first(facts.get("units_or_locations"), locations.get("count")), metadata(locations)),
            ("employees", first(facts.get("employees") if not group("employees") else None,
                                group("employees").get("value")), metadata(group("employees"))),
            ("auv", first(facts.get("auv_usd"), rev.get("auv_usd")), None),
        ):
            if isinstance(value, str) and signal != "auv":
                match = re.search(r"[\d,]+", value)
                value = int(match.group(0).replace(",", "")) if match else value
            if value is not None:
                write(signal, value, details=details, rejection=signal_error(signal, value))

        extras = facts.get("additional_claims")
        for extra in extras if isinstance(extras, list) else []:
            if isinstance(extra, dict) and isinstance(extra.get("signal"), str) and slugify(extra["signal"]):
                kind = extra.get("kind")
                if kind in {"measured", "reported", "estimate", "judgment"}:
                    value = extra.get("value")
                    if isinstance(value, str) or valid_number(value):
                        write("context_" + slugify(extra["signal"]), value, kind, details=extra)
        return rejected

    # ------------------------------------------------------------------ financials + scoring
    def _claim_for_snapshot(self, cid: int, row: dict) -> int:
        if row["claim_id"] is not None:
            return row["claim_id"]
        signal = row["signal"]
        kind = ("judgment" if signal in JUDGMENTS | {"founder_owned", "recurring_model"}
                else "estimate" if signal == "revenue_growth"
                else "reported" if row["source"] in {"seed", "llm"} else "measured")
        return self.db.record_claim(cid, signal, row["value"], kind=kind, source=row["source"] or "legacy",
                                    passage="Legacy snapshot. Claim-level citation was not recorded.",
                                    retrieved_at=row["ts"], snapshot_ts=row["ts"])

    def _financial_inputs(self, cid: int, claims: list[dict] | None = None) -> dict:
        # Old _facts snapshots contain a merged blob, so a model amount and its URL may
        # come from different runs. Keep attributable seed amounts and flag legacy model amounts.
        # Other numeric proxies can be used with explicit missing evidence.
        keys = CITED_REVENUE | {"units", "employees", "auv", "review_count", "revenue_growth"}
        accepted = {}
        for claim in self.db.claims(cid) if claims is None else claims:
            if not claim["rejection"] and claim["signal"] in keys:
                accepted.setdefault(claim["signal"], claim)
        if keys <= accepted.keys():
            return accepted
        for row in reversed(self.db.snapshot_history(cid, "_facts")):
            raw = json.loads(row["raw"])
            for signal in keys - accepted.keys():
                value = raw.get("growth" if signal == "revenue_growth" else signal)
                if value is None:
                    continue
                field = {"reported_revenue": "reported_source", "press_estimate": "press_source"}.get(signal)
                url = source_url(raw.get(field))
                rejection = None
                if signal in CITED_REVENUE and row["source"] == "llm":
                    rejection = "Legacy model revenue has no claim-specific provenance. Re-enrich to verify it."
                if signal == "inc_band":
                    if not (isinstance(value, list) and len(value) == 2 and all(valid_number(x) for x in value)
                            and 0 < value[0] <= value[1]):
                        continue
                elif signal_error(signal, value):
                    continue
                claim_id = self.db.record_claim(
                    cid, signal, value, kind="estimate" if signal in {"press_estimate", "revenue_growth"} else "reported",
                    source=row["source"] or "legacy", source_url=url if not rejection else None,
                    passage="Recovered from a legacy financial snapshot.", rejection=rejection,
                    retrieved_at=row["ts"], snapshot_ts=row["ts"])
                if not rejection:
                    accepted[signal] = {"id": claim_id, "value": value, "source_url": url}
        return accepted

    def _financials(self, cid: int, model: str | None, observed: dict) -> tuple[dict, int | None]:
        facts = {key: row["value"] for key, row in observed.items()}
        for signal, key in (("reported_revenue", "reported_source"), ("press_estimate", "press_source"),
                            ("system_sales", "system_sales_source"), ("inc_band", "inc_band_source")):
            if signal in observed:
                facts[key] = observed[signal]["source_url"]
        view = triangulate(facts, model).as_dict() | {
            "system_sales": facts.get("system_sales"), "growth": facts.get("revenue_growth"),
        }
        if view["mid"] is None:
            return view, None
        method_inputs = {
            "reported": ("reported_revenue",), "press_estimate": ("press_estimate",),
            "system_sales": ("system_sales",), "inc5000_band": ("inc_band",),
            "headcount": ("employees",), "units_x_auv": ("units", "auv"), "reviews": ("review_count",),
        }
        inputs = tuple(dict.fromkeys(observed[key]["id"] for method in view["methods"]
                                     for key in method_inputs[method["method"]] if key in observed))
        claim_id = self.db.record_claim(cid, "revenue_triangulation", view, kind="estimate", source="levi",
                                        passage="Methods: " + ", ".join(m["method"] for m in view["methods"]),
                                        inputs=inputs)
        return view, claim_id

    def financials(self, cid: int) -> dict:
        row = self.db.company(cid)
        return self._financials(cid, row["model"], self._financial_inputs(cid))[0]

    def _thesis_for(self, row) -> dict:
        try:
            return load_thesis(row["thesis"]) if row["thesis"] else load_thesis("adhoc")
        except Exception:
            return load_thesis("adhoc")

    def score_company(self, cid: int):
        row = self.db.company(cid)
        thesis = self._thesis_for(row)
        vals, claim_ids, issues = {}, {}, []
        snapshots = self.db.signal_rows(cid)
        ledger = self.db.claims(cid)
        claims = {r["id"]: r for r in ledger}
        for snapshot in snapshots:
            signal, value = snapshot["signal"], snapshot["value"]
            if signal not in BY_ID:
                continue
            error = signal_error(signal, value)
            claim = claims.get(snapshot["claim_id"])
            if claim and (claim["value"] != value or claim["rejection"]):
                error = "Snapshot does not match its accepted ledger claim"
            if error:
                issues.append(f"{signal} excluded: {error}")
                continue
            vals[signal] = bool(value) if BY_ID[signal].norm == "bool" else value
            claim_ids[signal] = self._claim_for_snapshot(cid, snapshot)

        observed = self._financial_inputs(cid, ledger)
        for signal in ("review_count", "revenue_growth"):
            # Prefer the exact accepted signal snapshot used by this score.
            if signal in claim_ids:
                observed[signal] = {"id": claim_ids[signal], "value": vals[signal]}
            elif any(r["signal"] == signal for r in snapshots):
                observed.pop(signal, None)
        fin, revenue_id = self._financials(cid, row["model"], observed)
        if revenue_id is not None:
            band = tuple(thesis.get("revenue_band", (5e6, 100e6)))
            growth = observed.get("revenue_growth")
            fit = revenue_fit(fin["mid"], growth["value"] if growth else None, band)
            for signal, value, passage, inputs in (
                ("revenue_fit", fit, f"Revenue range midpoint against thesis band {band}.",
                 (revenue_id, growth["id"]) if growth and growth["value"] > 0 else (revenue_id,)),
                ("revenue_confidence", fin["confidence"], "Confidence from triangulation methods.", (revenue_id,)),
                ("margin_profile", fin["indicative_pnl"]["ebitda_margin"] / 0.35,
                 fin["indicative_pnl"]["basis"], ()),
            ):
                if value is None:
                    continue
                vals[signal] = value
                claim_ids[signal] = self.db.record_claim(cid, signal, value, kind="estimate", source="levi",
                                                         passage=passage, inputs=inputs)
        card = score(vals, thesis.get("pillar_weights"))
        self.db.save_score(cid, card, [claim_ids[sid] for sid, _, _ in card.contributions], issues, fin)
        return card

    # ------------------------------------------------------------------ memo
    def memo(self, cid: int) -> dict:
        self.score_company(cid)
        d = self.dossier(cid)
        evidence = d["evidence"]
        claims = {r["id"]: r for r in evidence["claims"] + evidence["scored_claims"]}
        fields = ("signal", "claim", "value", "kind", "source", "source_url", "passage", "period",
                  "published_at", "retrieved_at", "missing_evidence", "inputs")
        context = json.dumps({
            "company": d["company"], "score": d["score"],
            "claims": {i: {key: r[key] for key in fields} for i, r in claims.items()},
            "rejections": [{key: r[key] for key in ("signal", "rejection")} for r in evidence["rejections"]],
            "validation_issues": evidence["validation_issues"],
        }, ensure_ascii=False)
        review = self.llm.ask(P.challenge_prompt(context), P.CHALLENGE_SCHEMA, purpose="challenge",
                              tier="deep", web=True, system=P.SYSTEM)
        objections = review.get("objections") if isinstance(review, dict) else None
        if not isinstance(objections, list):
            raise LLMError("Challenge did not return an objections list")
        for item in objections:
            if (not isinstance(item, dict) or item.get("kind") not in P.OBJECTION_KINDS
                    or not isinstance(item.get("concern"), str) or not item["concern"].strip()
                    or not isinstance(item.get("claim_ids"), list)
                    or any(type(i) is not int or i not in claims for i in item["claim_ids"])):
                raise LLMError("Challenge returned an invalid objection or unknown claim ID")
        missing = [r["id"] for r in evidence["scored_without_source"] if not r["inputs"]]
        if missing:
            objections = objections + [{"kind": "missing_evidence", "claim_ids": missing,
                                         "concern": "These scoring inputs have no direct source URL."}]
        if evidence["validation_issues"]:
            objections = objections + [{"kind": "unsupported_claim", "claim_ids": [],
                                         "concern": " ".join(evidence["validation_issues"])}]
        challenge = {"objections": [item | {"id": f"C{n}"} for n, item in enumerate(objections, 1)],
                     "evidence_ids": list(claims)}
        challenge_ts = self.db.snap(cid, "_challenge", None, raw=challenge, source=self.llm.backend)
        out = self.llm.ask(P.memo_prompt(context, json.dumps(challenge)), P.MEMO_SCHEMA,
                           purpose="memo", tier="deep", web=False, system=P.SYSTEM)
        self._validate_memo(out, challenge)
        self.db.snap(cid, "_memo", None, raw={"challenge_ts": challenge_ts, "memo": out}, source=self.llm.backend)
        return out

    @staticmethod
    def _validate_memo(memo, challenge: dict) -> None:
        if not isinstance(memo, dict):
            raise LLMError("Memo did not return an object")
        for key in ("headline", "thesis", "outreach_hook"):
            if not isinstance(memo.get(key), str):
                raise LLMError(f"Memo did not return {key}")
        for key in ("why_now", "value_creation", "key_risks", "diligence_questions"):
            if not isinstance(memo.get(key), list) or any(not isinstance(x, str) for x in memo[key]):
                raise LLMError(f"Memo did not return {key} as a list of strings")
        remaining = {item["id"] for item in challenge["objections"]}
        responses = memo.get("challenge_responses")
        if not isinstance(responses, list):
            raise LLMError("Memo did not return objection responses")
        for item in responses:
            if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                    or item["id"] not in remaining or item.get("disposition") not in ("answered", "downgraded")
                    or not isinstance(item.get("response"), str) or not item["response"].strip()):
                raise LLMError("Memo returned an invalid or duplicate objection response")
            remaining.remove(item["id"])
        if remaining:
            raise LLMError("Memo left unanswered objections: " + ", ".join(sorted(remaining)))

    def stored_memo(self, cid: int, signals: dict) -> tuple[dict | None, dict | None]:
        record = signals.get("_memo") or {}
        if "challenge_ts" in record:
            challenge = self.db.snapshot(cid, "_challenge", record["challenge_ts"])
            if challenge is not None:
                return record["memo"], challenge
        return None, signals.get("_challenge")

    def dossier(self, cid: int) -> dict:
        row = self.db.company(cid)
        sig = self.db.latest_signals(cid)
        prof = json.loads(row["profile"] or "{}")
        prof.pop("site_text", None)
        latest = self.db.latest_score(cid)
        scorecard = json.loads(latest["card"]) if latest else {}
        memo, challenge = self.stored_memo(cid, sig)
        return {
            "company": {k: row[k] for k in ("name", "website", "city", "state", "category", "model", "ownership",
                                            "thesis", "status")},
            "score": ({"levi_score": latest["score"], "levi_alpha": latest["alpha"], "coverage": latest["coverage"],
                       **scorecard, "alpha_experimental": True} if latest else None),
            "evidence": self._evidence(cid, latest),
            "signals": {k: v for k, v in sig.items() if not k.startswith("_")},
            "financials": (self.db.snapshot(cid, "_financials", latest["ts"])
                           if "evidence_ids" in scorecard else sig.get("_financials")),
            "profile": prof,
            "memo": memo,
            "challenge": challenge,
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
        self.log(f"[6/6] challenges and memos for top {len(final)}")
        for r in final:
            try:
                self.memo(r["id"])
            except (LLMError, BudgetExceeded) as e:
                self.log(f"    ! {e}")
                break
        return self.db.leaderboard(thesis["name"], by="alpha")
