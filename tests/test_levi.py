import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from levi import financials as F
from levi.config import Settings
from levi.llm import LLM, BudgetExceeded, extract_json, strict_schema
from levi.pipeline import Levi, load_thesis
from levi.scoring import bayesian_rating, revenue_fit, score
from levi.signals import BY_ID, SIGNALS

ROOT = Path(__file__).resolve().parents[1]


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
