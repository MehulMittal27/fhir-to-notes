"""ICD-10-CM match classification. NLM is mocked; no network."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import term_lookup  # noqa: E402

GM = "http://fhir.de/CodeSystem/bfarm/icd-10-gm"


def nlm_rows(*rows):
    return [len(rows), [r[0] for r in rows], None, [list(r) for r in rows]]


class ResolveIcdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "cache.json"

    def tearDown(self):
        self.tmp.cleanup()

    def resolve(self, code, response):
        with mock.patch.object(term_lookup, "_get_json", return_value=response) as get:
            result = term_lookup.resolve(GM, code, self.cache)
        return result, get

    def test_exact_match_is_exact(self):
        result, _ = self.resolve("Q01.1", nlm_rows(("Q01.1", "Made-up exact"),
                                                   ("Q01.10", "Made-up child")))
        self.assertEqual(result["match"], "exact")
        self.assertEqual(result["matched_code"], "Q01.1")
        self.assertEqual(result["en"], "Made-up exact")
        self.assertNotIn("review_reason", result)

    def test_prefix_match_is_flagged_for_review(self):
        result, _ = self.resolve("Q02", nlm_rows(("Q02.2", "Made-up left"),
                                                 ("Q02.1", "Made-up right")))
        self.assertEqual(result["match"], "prefix")
        self.assertEqual(result["matched_code"], "Q02.2")
        self.assertEqual(result["cm_candidates"], ["Q02.1", "Q02.2"])
        self.assertIn("Q02.2", result["review_reason"])
        self.assertIn("needs human review", result["review_reason"])

    def test_no_match_returns_none_and_is_not_cached(self):
        result, _ = self.resolve("Q03.09", nlm_rows(("Q04.1", "Unrelated")))
        self.assertIsNone(result)
        self.assertFalse(self.cache.exists())

    def test_cache_hit_needs_no_network(self):
        self.resolve("Q01.1", nlm_rows(("Q01.1", "Made-up exact")))
        result, get = self.resolve("Q01.1", None)
        get.assert_not_called()
        self.assertEqual(result["match"], "exact")

    def test_legacy_cached_prefix_hit_is_classified_as_prefix(self):
        self.cache.write_text(json.dumps({f"{GM}|Q05": {
            "en": "Made-up child", "matched_code": "Q05.1",
            "source": "NLM Clinical Tables (ICD-10-CM Q05.1, nearest match for Q05)"}}))
        result, get = self.resolve("Q05", None)
        get.assert_not_called()
        self.assertEqual(result["match"], "prefix")
        self.assertEqual(result["cm_candidates"], ["Q05.1"])
        self.assertIn("review_reason", result)


if __name__ == "__main__":
    unittest.main()
