import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from levi import financials as F
from levi.config import Settings
from levi.llm import LLM, BudgetExceeded, LLMError, extract_json, strict_schema
from levi.pipeline import Levi, load_thesis
from levi.report import dossier_md
from levi.scoring import bayesian_rating, revenue_fit, score
from levi.signals import BY_ID, SIGNALS

ROOT = Path(__file__).resolve().parents[1]
HOUNDS_FIXTURE = ROOT / "tests/fixtures/hounds_town_fdd.json"


def settings(tmp):
    s = Settings()
    s.home = Path(tmp)
    s.backend = "codex"
    s.max_llm_calls = 2
    return s


class SignalTests(unittest.TestCase):
    def test_ids_unique_and_normalise_bounds(self):
        self.assertEqual(len({s.id for s in SIGNALS}), len(SIGNALS))
        for s in SIGNALS:
            for v in (s.lo, s.hi, -1e9, 1e9):
                n = s.normalise(True if s.norm == "bool" else v)
                self.assertTrue(0.0 <= n <= 1.0, s.id)
            self.assertIsNone(s.normalise(None))

    def test_lower_is_better(self):
        s = BY_ID["institutional_awareness"]
        self.assertGreater(s.normalise(0.1), s.normalise(0.9))


class ScoringTests(unittest.TestCase):
    def test_bayesian_rating_shrinks_small_samples(self):
        self.assertLess(bayesian_rating(5.0, 3), 4.4)
        self.assertGreater(bayesian_rating(5.0, 5000), 4.95)

    def test_revenue_fit_trajectory(self):
        band = (10e6, 100e6)
        self.assertEqual(revenue_fit(50e6, None, band), 1.0)
        self.assertLess(revenue_fit(3e6, None, band), 0.6)
        # small but doubling -> lands in band within 2 years
        self.assertGreater(revenue_fit(3e6, 1.0, band), 0.85)

    def test_more_evidence_and_better_signals_score_higher(self):
        weak = score({"founder_owned": True})
        strong = score({"founder_owned": True, "review_rating": 4.85, "review_count": 8000,
                        "revenue_growth": 0.8, "social_followers": 400000, "ai_share_of_voice": 0.5,
                        "institutional_awareness": 0.1, "revenue_fit": 1.0})
        self.assertGreater(strong.score, weak.score)
        self.assertGreater(strong.coverage, weak.coverage)

    def test_alpha_penalises_known_and_sponsored(self):
        base = {"review_rating": 4.8, "review_count": 3000, "revenue_growth": 0.6}
        hidden = score(base | {"institutional_awareness": 0.1, "founder_owned": True})
        shopped = score(base | {"institutional_awareness": 0.9, "founder_owned": False})
        self.assertGreater(hidden.alpha, shopped.alpha)


class FinancialTests(unittest.TestCase):
    def test_reported_dominates_proxies(self):
        v = F.triangulate({"reported_revenue": 50e6, "employees": 38}, "dtc_consumer")
        self.assertGreater(v.mid, 42e6)
        self.assertGreaterEqual(v.confidence, 0.9)

    def test_franchisor_units_are_system_sales(self):
        v = F.triangulate({"units": 100, "auv": 1e6}, "franchisor")
        self.assertLess(v.high, 25e6)  # royalties, not $100M of system sales

    def test_empty(self):
        self.assertIsNone(F.triangulate({}, None).mid)

    def test_pnl_labelled_as_benchmark(self):
        p = F.indicative_pnl(10e6, "vertical_saas")
        self.assertIn("NOT company-reported", p["basis"])


class LLMTests(unittest.TestCase):
    def test_strict_schema(self):
        s = strict_schema({"type": "object", "properties": {"a": {"type": "object", "properties": {"b": {}}}}})
        self.assertFalse(s["additionalProperties"])
        self.assertEqual(s["properties"]["a"]["required"], ["b"])

    def test_extract_json(self):
        self.assertEqual(extract_json('noise ```json\n{"x": 1}\n``` tail'), {"x": 1})

    def test_codex_command_is_lean(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(tmp)
            from levi.db import DB
            llm = LLM(s, DB(s.db_path))
            cmd = llm.codex_cmd("s.json", "o.json", tier="fast", web=False, model="")
            self.assertEqual(cmd[:2], ["codex", "exec"])
            self.assertIn('model_reasoning_effort="low"', cmd)
            self.assertIn('web_search="disabled"', cmd)
            self.assertIn("read-only", cmd)

    def test_cache_and_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(tmp)
            from levi.db import DB
            llm = LLM(s, DB(s.db_path))
            with mock.patch.object(LLM, "_codex", return_value={"ok": 1}) as m:
                schema = {"type": "object", "properties": {"ok": {"type": "integer"}}}
                self.assertEqual(llm.ask("q1", schema, purpose="t"), {"ok": 1})
                self.assertEqual(llm.ask("q1", schema, purpose="t"), {"ok": 1})  # cache hit
                self.assertEqual(m.call_count, 1)
                llm.ask("q2", schema, purpose="t")
                with self.assertRaises(BudgetExceeded):
                    llm.ask("q3", schema, purpose="t")


class PipelineTests(unittest.TestCase):
    def test_collector_keeps_measured_evidence_with_the_request_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            levi = Levi(settings(tmp), log=lambda m: None)
            cid = levi.db.upsert_company({"name": "Example Brand"})

            def response(url, **kwargs):
                if "wikipedia" in url:
                    return {"query": {"pages": {"123": {}}}}
                if "algolia" in url:
                    return {"nbHits": None}
                return None

            with mock.patch.object(levi.http, "json", side_effect=response):
                levi.collect_free(cid)
            evidence = levi.evidence(cid)
            claim = next(c for c in evidence["claims"] if c["signal"] == "wikipedia_page")
            self.assertEqual(claim["kind"], "measured")
            self.assertIn("titles=Example%20Brand", claim["source_url"])
            self.assertEqual(set(claim) & {"period", "published_at", "retrieved_at"},
                             {"period", "published_at", "retrieved_at"})
            self.assertEqual(evidence["source_url_coverage"], 1.0)
            self.assertEqual([r["signal"] for r in evidence["rejections"]], ["hn_hits"])

    def test_enrichment_rejects_unsourced_revenue_and_labels_judgments(self):
        with tempfile.TemporaryDirectory() as tmp:
            messages = []
            levi = Levi(settings(tmp), log=messages.append)
            cid = levi.db.upsert_company({"name": "Example Franchise", "model": "franchisor"})
            with mock.patch.object(levi.llm, "ask", return_value={
                "revenue_reported_usd": 99_000_000,
                "third_party_revenue_estimate_usd": 88_000_000,
                "system_sales_usd": 77_000_000,
                "pricing_power": 0.7,
                "fad_risk": 1.5,
            }):
                levi.enrich(cid)
            self.assertIsNone(levi.financials(cid)["mid"])
            evidence = levi.evidence(cid)
            self.assertEqual({r["signal"] for r in evidence["rejections"]},
                             {"reported_revenue", "press_estimate", "system_sales", "fad_risk"})
            judgment = next(c for c in evidence["claims"] if c["signal"] == "pricing_power")
            self.assertEqual(judgment["kind"], "judgment")
            self.assertTrue(judgment["missing_evidence"])
            self.assertTrue(any("reported_revenue rejected" in m for m in messages))

    def test_legacy_seed_blob_does_not_relabel_carried_forward_model_revenue(self):
        with tempfile.TemporaryDirectory() as tmp:
            levi = Levi(settings(tmp), log=lambda m: None)
            cid = levi.db.upsert_company({"name": "Legacy Brand"})
            inherited = {"reported_revenue": 99_000_000}
            levi.db.snap(cid, "_facts", None, source="llm", raw=inherited)
            levi.db.snap(cid, "_facts", None, source="seed", raw=inherited | {"employees": 2})
            levi.score_company(cid)
            self.assertEqual([m["method"] for m in levi.dossier(cid)["financials"]["methods"]], ["headcount"])
            self.assertEqual([r["signal"] for r in levi.evidence(cid)["rejections"]], ["reported_revenue"])

    def test_invalid_revenue_does_not_replace_an_accepted_amount(self):
        with tempfile.TemporaryDirectory() as tmp:
            levi = Levi(settings(tmp), log=lambda m: None)
            cid = levi.db.upsert_company({"name": "Example Brand"})
            source = "https://example.com/financials"
            for amount in (10_000_000, 1e-200):
                with mock.patch.object(levi.llm, "ask", return_value={
                    "revenue_reported_usd": amount, "revenue_source_url": source,
                    "evidence": [{"signal": "reported_revenue", "source_url": None, "period": "FY2023"}],
                }):
                    levi.enrich(cid)
            levi.score_company(cid)
            evidence = levi.evidence(cid)
            cited = next(r for r in evidence["scored_claims"] if r["signal"] == "reported_revenue")
            self.assertEqual(cited["value"], 10_000_000)
            self.assertEqual(cited["source_url"], source)
            self.assertEqual(evidence["rejections"][0]["value"], 1e-200)

    def test_review_velocity_requires_comparable_observations_and_records_the_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            levi = Levi(settings(tmp), log=lambda m: None)
            cid = levi.db.upsert_company({"name": "Example Brand"})
            first = datetime.fromisoformat("2024-04-01T00:00:00+00:00").timestamp()
            observations = [(10, "https://reviews.example/a"), (20, "https://reviews.example/a"),
                            (200, "https://reviews.example/b")]
            for i, (count, url) in enumerate(observations):
                with mock.patch("levi.db.time.time", return_value=first + i * 86400 * 10), \
                        mock.patch.object(levi.llm, "ask", return_value={
                            "review_count": count, "evidence": [{"signal": "review_count", "source_url": url}],
                        }):
                    levi.enrich(cid)
            latest = levi.evidence(cid)["claims"]
            velocity = next(r for r in latest if r["signal"] == "review_velocity")
            self.assertEqual(velocity["value"], 30)
            self.assertIn("2024-04-01", velocity["period"])
            self.assertIn("2024-04-11", velocity["period"])
            self.assertEqual(len(velocity["inputs"]), 2)
            self.assertNotIn("review_velocity", {s for s, *_ in levi.score_company(cid).contributions})

    def test_consumer_seed_dossier_shows_experimental_alpha_and_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            levi = Levi(settings(tmp), log=lambda m: None)
            self.assertEqual(levi.import_seeds(str(ROOT / "data/seeds/consumer_brands.json")), 12)
            cid = levi.db.companies()[0]["id"]
            card = levi.score_company(cid)
            dossier = levi.dossier(cid)
            breakdown = dossier["score"]["alpha_breakdown"]
            self.assertEqual(round(breakdown["fit"] * breakdown["institutional_unawareness_factor"]
                                   * breakdown["founder_factor"], 1), card.alpha)
            self.assertTrue(dossier["evidence"]["scored_claims"])
            self.assertFalse(dossier["evidence"]["validation_issues"])
            md = dossier_md(dossier)
            self.assertIn("Levi Alpha (experimental)", md)
            self.assertIn("## Evidence", md)
            self.assertIn("## Challenge", md)

    def test_memo_requires_a_stored_challenge_and_addresses_each_objection(self):
        with tempfile.TemporaryDirectory() as tmp:
            levi = Levi(settings(tmp), log=lambda m: None)
            cid = levi.db.upsert_company({"name": "Example Brand"})
            challenge = {"objections": [{"kind": "reason_to_pass", "concern": "Customer retention is unknown.",
                                         "claim_ids": []}]}
            with mock.patch.object(levi.llm, "ask", side_effect=LLMError("challenge failed")) as ask:
                with self.assertRaises(LLMError):
                    levi.memo(cid)
            self.assertEqual([c.kwargs["purpose"] for c in ask.call_args_list], ["challenge"])
            self.assertIsNone(levi.dossier(cid)["memo"])

            def respond(prompt, schema, *, purpose, **kwargs):
                if purpose == "challenge":
                    return challenge
                self.assertIn("Customer retention is unknown", prompt)
                self.assertIsNotNone(levi.dossier(cid)["challenge"])
                return {"headline": "Investigate", "thesis": "Early screen", "why_now": [],
                        "value_creation": [], "key_risks": [], "diligence_questions": [], "outreach_hook": "",
                        "challenge_responses": []}

            with mock.patch.object(levi.llm, "ask", side_effect=respond):
                with self.assertRaisesRegex(LLMError, "objection"):
                    levi.memo(cid)
            self.assertIsNone(levi.dossier(cid)["memo"])

    def test_hounds_town_fdd_keeps_period_scope_and_audit_status_in_memo_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            levi = Levi(settings(tmp), log=lambda m: None)
            simulated_retrieval = datetime.fromisoformat("2024-04-22T16:00:00+00:00").timestamp()
            with mock.patch("levi.db.time.time", return_value=simulated_retrieval):
                levi.import_seeds(str(HOUNDS_FIXTURE))
            cid = levi.db.company("Hounds Town USA")["id"]
            self.assertFalse(levi.evidence(cid, as_of=simulated_retrieval - 1)["claims"])
            claims = {r["signal"]: r for r in levi.evidence(cid, as_of=simulated_retrieval)["claims"]}
            self.assertEqual(claims["units"]["value"], 55)
            self.assertEqual(claims["context_outlet-reporting-group"]["value"], 37)
            self.assertEqual(claims["reported_revenue"]["value"], 3_386_658)
            self.assertEqual(claims["context_franchisor-net-loss-usd"]["value"], -735_854)
            self.assertEqual(claims["reported_revenue"]["period"], "FY2023")
            self.assertEqual(claims["reported_revenue"]["published_at"], "2024-04-20")
            self.assertNotIn("system_sales", claims)

            source = claims["reported_revenue"]["source_url"]

            def respond(prompt, schema, *, purpose, **kwargs):
                for detail in ("2023-12-31", "FY2023", "unaudited", "audited Statements of Operations",
                               "Item 19", "Item 21", "B-7", source):
                    self.assertIn(detail, prompt)
                if purpose == "challenge":
                    return {"objections": [{"kind": "contradiction", "concern": "Do not generalize the outlet sample.",
                                             "claim_ids": [claims["context_outlet-reporting-group"]["id"]]}]}
                return {
                    "headline": "Require current financials",
                    "thesis": f"FY2023 audited franchisor operating revenue was ${3_386_658:,}, with a "
                              f"${735_854:,} net loss (Item 21, Exhibit B, p. B-7, {source}).",
                    "why_now": [f"The unaudited Item 19 cohort covered 37 outlets. The network had 55 outlets "
                                f"at December 31, 2023 ({source})."],
                    "value_creation": [], "key_risks": [], "diligence_questions": [], "outreach_hook": "",
                    "challenge_responses": [
                        {"id": "C1", "disposition": "answered", "response": "The memo states each scope separately."},
                        {"id": "C2", "disposition": "downgraded", "response": "Margin assumption is illustrative."},
                    ],
                }

            with mock.patch.object(levi.llm, "ask", side_effect=respond):
                levi.memo(cid)
            dossier = levi.dossier(cid)
            self.assertEqual([m["method"] for m in dossier["financials"]["methods"]], ["reported"])
            md = dossier_md(dossier)
            self.assertIn("Challenge responses", md)
            self.assertIn("55 outlets", md)
            self.assertIn("37 outlets", md)
            self.assertIn("audited franchisor operating revenue", md)
            self.assertIn("unaudited Item 19 cohort", md)
            self.assertIn("$3,386,658", md)

    def test_memo_retry_uses_the_stored_challenge_without_another_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(tmp)
            s.max_llm_calls = 1
            s.llm_cache_days = 0
            levi = Levi(s, log=lambda m: None)
            levi.import_seeds(str(HOUNDS_FIXTURE))
            cid = levi.db.company("Hounds Town USA")["id"]
            with mock.patch.object(levi.llm, "_codex", return_value={"objections": []}):
                with self.assertRaises(BudgetExceeded):
                    levi.memo(cid)
            self.assertIsNotNone(levi.dossier(cid)["challenge"])

            levi = Levi(s, log=lambda m: None)
            memo = {"headline": "Diligence required", "thesis": "Historical data", "why_now": [],
                    "value_creation": [], "key_risks": [], "diligence_questions": [], "outreach_hook": "",
                    "challenge_responses": [
                        {"id": "C1", "disposition": "downgraded", "response": "Margin is only a benchmark."},
                    ]}
            with mock.patch.object(levi.llm, "_codex", return_value=memo) as model:
                self.assertEqual(levi.memo(cid), memo)
            self.assertIn("Stored challenge", model.call_args.args[0])
            self.assertEqual(levi.llm.calls, 1)

    def test_pending_review_keeps_prior_memo_and_its_cited_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            levi = Levi(settings(tmp), log=lambda m: None)
            cid = levi.db.upsert_company({"name": "Example Brand"})
            with mock.patch.object(levi.llm, "ask", return_value={"pricing_power": 0.7}):
                levi.enrich(cid)
            prior_id = levi.evidence(cid)["claims"][0]["id"]
            challenge = {"objections": [{"kind": "unsupported_claim", "concern": "Pricing power needs evidence.",
                                         "claim_ids": [prior_id]}]}
            memo = {"headline": "Prior memo", "thesis": "Pricing power unproven", "why_now": [],
                    "value_creation": [], "key_risks": [], "diligence_questions": [], "outreach_hook": "",
                    "challenge_responses": [
                        {"id": f"C{i}", "disposition": "downgraded", "response": "Pricing power needs evidence."}
                        for i in (1, 2)
                    ]}
            with mock.patch.object(levi.llm, "ask", side_effect=[challenge, memo]):
                levi.memo(cid)
            with mock.patch.object(levi.llm, "ask", return_value={"pricing_power": 0.3}):
                levi.enrich(cid)
            with mock.patch.object(levi.llm, "ask", side_effect=[{"objections": []}, BudgetExceeded("limit")]):
                with self.assertRaises(BudgetExceeded):
                    levi.memo(cid)
            dossier = levi.dossier(cid)
            self.assertEqual(dossier["memo"]["headline"], "Prior memo")
            self.assertEqual(dossier["memo_challenge"]["objections"][0]["claim_ids"], [prior_id])
            self.assertIn(prior_id, {r["id"] for r in dossier["evidence"]["reviewed_claims"]})
            self.assertIn("Latest challenge awaiting a memo", dossier_md(dossier))

    def test_as_of_evidence_excludes_later_publication_and_its_derivations(self):
        with tempfile.TemporaryDirectory() as tmp:
            levi = Levi(settings(tmp), log=lambda m: None)
            cid = levi.db.upsert_company({"name": "Example Brand"})
            cutoff = datetime.fromisoformat("2024-04-21T23:59:59+00:00").timestamp()
            original = levi.db.record_claim(cid, "units", 50, kind="reported", source="fixture",
                                            source_url="https://example.com/old", published_at="2024-04-01",
                                            retrieved_at=cutoff - 2)
            later = levi.db.record_claim(cid, "units", 55, kind="reported", source="fixture",
                                         source_url="https://example.com/later", published_at="2024-04-22",
                                         retrieved_at=cutoff - 1)
            levi.db.record_claim(cid, "revenue_triangulation", {}, kind="estimate", source="fixture",
                                 retrieved_at=cutoff, inputs=(later,))
            claims = levi.evidence(cid, as_of=cutoff)["claims"]
            self.assertEqual([r["id"] for r in claims], [original])

    def test_consumer_seed_cli_smoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ | {"LEVI_HOME": tmp}

            def cli(*args):
                proc = subprocess.run([sys.executable, "-m", "levi", *args],
                                      cwd=ROOT, env=env, check=True, capture_output=True, text=True)
                return proc.stdout

            company = "Fishwife Tinned Seafood Co."
            self.assertIn("imported 12 companies", cli("import", "data/seeds/consumer_brands.json"))
            markdown = cli("show", company, "--md")
            for phrase in ("## Evidence", "## Challenge", "institutional-unawareness factor", "Source URL coverage"):
                self.assertIn(phrase, markdown)
            self.assertTrue(json.loads(cli("evidence", company, "--json"))["scored_claims"])
            self.assertIn("Alpha (experimental)", cli("top"))

    def test_import_seeds_and_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            levi = Levi(settings(tmp), log=lambda m: None)
            n = sum(levi.import_seeds(str(p)) for p in (ROOT / "data" / "seeds").glob("*.json"))
            self.assertGreater(n, 30)
            for r in levi.db.companies():
                levi.score_company(r["id"])
            top = levi.db.leaderboard(limit=5)
            self.assertEqual(len(top), 5)
            d = levi.dossier(top[0]["id"])
            self.assertIn("financials", d)

    def test_ai_probe_scores_share_of_voice(self):
        with tempfile.TemporaryDirectory() as tmp:
            levi = Levi(settings(tmp), log=lambda m: None)
            levi.s.max_llm_calls = 10
            t = load_thesis("vertical-saas")
            levi.db.upsert_company({"name": "JobTread", "thesis": t["name"]})
            levi.db.upsert_company({"name": "Nobody Inc", "thesis": t["name"]})
            with mock.patch.object(LLM, "_codex", return_value={"recommendations": ["JobTread", "Buildertrend"]}):
                out = levi.ai_probe(t)
            self.assertEqual(out["share_of_voice"]["JobTread"], 1.0)
            self.assertEqual(out["share_of_voice"]["Nobody Inc"], 0.0)
            self.assertIn("Buildertrend", out["untracked_ai_recommendations"])

    def test_free_text_thesis(self):
        t = load_thesis("independent veterinary clinics in Texas")
        self.assertIn("veterinary", t["description"])

    def test_seed_files_have_sources(self):
        for p in (ROOT / "data" / "seeds").glob("*.json"):
            for c in json.loads(p.read_text()):
                self.assertTrue(c.get("name"))
                self.assertTrue(c.get("sources"), f"{c['name']} has no sources")


if __name__ == "__main__":
    unittest.main()
