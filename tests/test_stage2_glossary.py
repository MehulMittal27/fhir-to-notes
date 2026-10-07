"""Stage 2 routing of ICD-10-CM lookups. NLM and the LLM are mocked."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import stage2_glossary  # noqa: E402

GM = "http://fhir.de/CodeSystem/bfarm/icd-10-gm"

LOOKUPS = {
    "Q01.1": {"en": "Made-up exact", "matched_code": "Q01.1", "match": "exact",
              "source": "NLM Clinical Tables (ICD-10-CM)"},
    "Q02": {"en": "Made-up right", "matched_code": "Q02.1", "match": "prefix",
            "cm_candidates": ["Q02.1", "Q02.2"],
            "review_reason": "ICD-10-GM Q02 has no exact ICD-10-CM entry; ...",
            "source": "NLM Clinical Tables (ICD-10-CM Q02.1, nearest match for Q02)"},
}


def concept(code, display):
    return {"system": GM, "code": code, "display": display,
            "tier": "code_lookup", "resource_types": ["Condition"]}


class Stage2IcdRoutingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.out, self.cfg = root / "out", root / "config"
        self.out.mkdir()
        self.cfg.mkdir()
        (self.cfg / "icd10_atc_en.json").write_text(json.dumps({"icd-10-gm": {}, "atc": {}}))
        self.inventory = root / "inventory.json"
        self.inventory.write_text(json.dumps({"concepts": [
            concept("Q01.1", "Erfundene Diagnose A"),
            concept("Q02", "Erfundene Diagnose B"),
            concept("Q03.09", "Erfundene Diagnose C"),
        ]}))

    def tearDown(self):
        self.tmp.cleanup()

    def run_stage2(self):
        argv = ["stage2", "--inventory", str(self.inventory), "--out", str(self.out),
                "--config", str(self.cfg)]
        llm = json.dumps([{"id": f"{GM}|Q03.09|Erfundene Diagnose C",
                           "en": "Made-up proposal", "confidence": "high"}])
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(stage2_glossary, "resolve_external",
                                  side_effect=lambda s, c, cache_path: LOOKUPS.get(c)) as ext, \
                mock.patch.object(stage2_glossary, "call_codex", return_value=llm), \
                mock.patch.object(stage2_glossary, "verify_drug_name", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()):
            stage2_glossary.main()
        terms = json.loads((self.cfg / "glossary.json").read_text())["terms"]
        return {cid.split("|")[1]: t for cid, t in terms.items()}, ext

    def test_exact_match_ships_auto(self):
        terms, _ = self.run_stage2()
        self.assertEqual(terms["Q01.1"]["status"], "auto")
        self.assertEqual(terms["Q01.1"]["match"], "exact")
        self.assertNotIn("review_reason", terms["Q01.1"])

    def test_prefix_match_goes_to_review_with_visible_decision(self):
        terms, _ = self.run_stage2()
        t = terms["Q02"]
        self.assertEqual(t["status"], "pending_review")
        self.assertEqual(t["en"], "Made-up right")
        self.assertEqual(t["matched_code"], "Q02.1")
        self.assertEqual(t["cm_candidates"], ["Q02.1", "Q02.2"])
        self.assertIn("review_reason", t)

    def test_unresolved_gm_only_code_goes_to_review(self):
        terms, _ = self.run_stage2()
        t = terms["Q03.09"]
        self.assertEqual(t["status"], "pending_review")
        self.assertEqual(t["en"], "Made-up proposal")
        self.assertIn("German-only", t["review_reason"])

    def test_legacy_auto_prefix_entry_is_not_carried_forward(self):
        legacy = {f"{GM}|Q02|Erfundene Diagnose B": {
            "en": "Made-up right", "status": "auto",
            "source": "NLM Clinical Tables (ICD-10-CM Q02.1, nearest match for Q02)"}}
        (self.cfg / "glossary.json").write_text(json.dumps({"terms": legacy}))
        terms, _ = self.run_stage2()
        self.assertEqual(terms["Q02"]["status"], "pending_review")

    def test_human_approved_prefix_entry_is_carried_forward(self):
        approved = {f"{GM}|Q02|Erfundene Diagnose B": {
            "en": "Reviewed term", "status": "approved", "match": "prefix",
            "matched_code": "Q02.1", "source": "reviewed"}}
        (self.cfg / "glossary.json").write_text(json.dumps({"terms": approved}))
        terms, ext = self.run_stage2()
        self.assertEqual(terms["Q02"]["status"], "approved")
        self.assertEqual(terms["Q02"]["en"], "Reviewed term")
        self.assertEqual(terms["Q02"]["matched_code"], "Q02.1")
        self.assertNotIn("Q02", [c.args[1] for c in ext.call_args_list])

    def test_glossary_terms_are_byte_stable_across_runs(self):
        self.run_stage2()
        first = json.loads((self.cfg / "glossary.json").read_text())["terms"]
        self.run_stage2()
        second = json.loads((self.cfg / "glossary.json").read_text())["terms"]
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
