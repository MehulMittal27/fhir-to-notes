"""Stage 4 assertion states (present / negated / uncertain), rendering and gate."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import stage4_convert as s4  # noqa: E402

GM = "http://fhir.de/CodeSystem/bfarm/icd-10-gm"
LOOKUP = {("icd-10", "Q01"): "Made-up disease",
          ("icd-10", "Q02"): "Made-up marker negative tumor",
          ("icd-10", "Q03"): "Made-up refuted disease",
          ("icd-10", "Q04"): "Made-up suspected disease"}


def condition(rid, code, display, **extra):
    return {"resource": {"resourceType": "Condition", "id": rid,
                         "code": {"coding": [{"system": GM, "code": code,
                                              "display": display}]}, **extra}}


def refuted_r4():
    return {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/"
                                  "condition-ver-status", "code": "refuted"}]}


def bundle(*entries):
    return {"resourceType": "Bundle", "entry": [
        {"resource": {"resourceType": "Patient", "id": "p1"}}, *entries]}


def by_term(facts):
    return {f["term"]: f for f in facts["conditions"]}


class AssertionTest(unittest.TestCase):
    def test_plain_condition_is_present(self):
        f = by_term(s4.collect_facts(bundle(condition("c1", "Q01", "Erfunden")), LOOKUP))
        self.assertEqual(f["Made-up disease"]["assertion"], "present")
        self.assertFalse(f["Made-up disease"]["negated"])

    def test_cue_in_concept_name_is_uncertain_not_negated(self):
        f = by_term(s4.collect_facts(bundle(
            condition("c2", "Q02", "Erfundener Marker-negativ Tumor")), LOOKUP))
        fact = f["Made-up marker negative tumor"]
        self.assertEqual(fact["assertion"], "uncertain")
        self.assertFalse(fact["negated"])
        self.assertIn("negativ", fact["assertion_evidence"])

    def test_refuted_verification_status_is_negated(self):
        f = by_term(s4.collect_facts(bundle(
            condition("c3", "Q03", "Erfunden", verificationStatus=refuted_r4())), LOOKUP))
        self.assertEqual(f["Made-up refuted disease"]["assertion"], "negated")
        self.assertTrue(f["Made-up refuted disease"]["negated"])

    def test_refuted_stu3_plain_code_is_negated(self):
        f = by_term(s4.collect_facts(bundle(
            condition("c3", "Q03", "Erfunden", verificationStatus="refuted")), LOOKUP))
        self.assertEqual(f["Made-up refuted disease"]["assertion"], "negated")

    def test_other_verification_statuses_stay_present(self):
        status = {"coding": [{"code": "provisional"}]}
        f = by_term(s4.collect_facts(bundle(
            condition("c4", "Q04", "Erfunden", verificationStatus=status)), LOOKUP))
        self.assertEqual(f["Made-up suspected disease"]["assertion"], "present")

    def test_cue_in_observation_interpretation_is_uncertain(self):
        obs = {"resource": {"resourceType": "Observation", "id": "o1",
                            "code": {"coding": [{"system": GM, "code": "Q01"}]},
                            "valueQuantity": {"value": 4, "unit": "U"},
                            "interpretation": [{"text": "negativ"}]}}
        facts = s4.collect_facts(bundle(obs), LOOKUP)
        self.assertEqual(facts["observations"][0]["assertion"], "uncertain")


class RenderAndGateTest(unittest.TestCase):
    """Bundles without birthDate/gender: conditions must still render."""

    def setUp(self):
        self.facts = s4.collect_facts(bundle(
            condition("c1", "Q01", "Erfunden"),
            condition("c2", "Q02", "Erfundener Marker-negativ Tumor"),
            condition("c3", "Q03", "Erfunden", verificationStatus=refuted_r4()),
        ), LOOKUP)

    def test_state_mode_never_states_uncertain_fact_as_absent(self):
        note, sanctioned = s4.render(self.facts, negation_mode="state")
        self.assertIn("History of Made-up disease.", note)
        self.assertIn("Made-up refuted disease: not detected", note)
        self.assertNotIn("Made-up marker negative tumor: not detected", note)
        self.assertIn(f"{s4.UNCERTAIN_LEAD}Made-up marker negative tumor.", note)
        verdict = s4.gate(note, self.facts, sanctioned)
        self.assertFalse(verdict["hard_fail"], verdict)
        self.assertEqual([u["term"] for u in verdict["uncertain_facts"]],
                         ["Made-up marker negative tumor"])

    def test_omit_mode_keeps_uncertain_fact_qualified_and_drops_negated(self):
        note, sanctioned = s4.render(self.facts, negation_mode="omit")
        self.assertNotIn("Made-up refuted disease", note)
        self.assertNotIn("not detected", note)
        self.assertIn(f"{s4.UNCERTAIN_LEAD}Made-up marker negative tumor.", note)
        verdict = s4.gate(note, self.facts, sanctioned)
        self.assertFalse(verdict["hard_fail"], verdict)
        self.assertEqual(verdict["dropped_facts"],
                         [{"term": "Made-up refuted disease", "reason": "negated"}])

    def test_gate_rejects_uncertain_fact_stated_absent(self):
        negatives = f"{s4.NEGATIVES_LEAD}Made-up marker negative tumor: not detected."
        note = "Patient with a history of Made-up disease. " + negatives
        verdict = s4.gate(note, self.facts, {"negatives": negatives})
        self.assertEqual(verdict["uncertain_stated_absent"], ["Made-up marker negative tumor"])
        self.assertTrue(verdict["hard_fail"])

    def test_gate_rejects_uncertain_fact_asserted_present(self):
        note = "Patient with a history of Made-up disease, Made-up marker negative tumor."
        verdict = s4.gate(note, self.facts, {})
        self.assertEqual(verdict["uncertain_asserted"], ["Made-up marker negative tumor"])
        self.assertTrue(verdict["hard_fail"])

    def test_gate_rejects_negated_fact_asserted_present(self):
        note = "Patient with a history of Made-up refuted disease."
        verdict = s4.gate(note, self.facts, {})
        self.assertEqual(verdict["negation_flips"], ["Made-up refuted disease"])
        self.assertTrue(verdict["hard_fail"])


if __name__ == "__main__":
    unittest.main()
