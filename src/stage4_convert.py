"""Stage 4: Guarded conversion - FHIR bundle -> English patient note.

Per patient:
  1. Deterministic fact extraction from the bundle (demographics, conditions,
     observations, procedures, medications). Terms resolve through the glossary.
  2. Negation rule engine: any evidence string containing a negation cue marks
     the fact negated; negated facts are omitted from the note and logged.
  3. Deterministic template rendering into TREC-style prose. No LLM writes prose.
  4. Faithfulness gate:
       - every number in the note must exist in the extracted facts
       - every glossary term used must be an approved/auto glossary entry
       - dropped (negated) facts are counted in the report
A failing gate blocks the note (written to out/rejected/, never out/notes/).

Optional --llm-polish routes only medication dosage schedules through Codex CLI,
then re-checks the number-subset rule; off by default to keep runs deterministic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fhirutil import sha256_file  # noqa: E402

NEG_CUES = ["no ", "denies ", "without ", "negative for", "ruled out",
            "not detected", "nicht nachweisbar", "n. nachweisbar",
            "kein nachweis", "keine ", "negativ"]
CODE_SYSTEMS_SHORT = {"icd-10-gm": "ICD-10", "atc": "ATC", "ops": "OPS",
                      "loinc.org": "LOINC", "sct": "SNOMED"}


def load_glossary(path: Path) -> tuple[dict[str, str], list[str]]:
    g = json.loads(path.read_text(encoding="utf-8"))
    lookup, unapproved = {}, []
    for cid, t in g["terms"].items():
        if not t.get("en"):
            continue
        if t["status"] not in ("auto", "approved"):
            unapproved.append(cid)
            continue
        key = (cid.split("|")[0].lower(), cid.split("|")[1])
        lookup[key] = t["en"]
        # also allow display-string lookup for hospital-internal systems
        lookup[("display", cid.split("|")[2].lower())] = t["en"]
    return lookup, unapproved


def term_for(bundle_res: dict, lookup: dict) -> str | None:
    """Resolve a resource's concept through the glossary; None if unresolvable."""
    candidates = []

    def walk(node):
        if isinstance(node, dict):
            for c in node.get("coding", []):
                sys_short = _sys_short(c.get("system", ""))
                if sys_short and c.get("code"):
                    candidates.append((sys_short.lower(), c["code"]))
                if c.get("display"):
                    candidates.append(("display", c["display"].lower()))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(node=bundle_res)
    for key in candidates:
        if key in lookup:
            return lookup[key]
    return None


def _sys_short(system: str) -> str | None:
    s = (system or "").lower()
    for long, short in CODE_SYSTEMS_SHORT.items():
        if long in s:
            return short
    return None


def is_negated(text: str) -> bool:
    t = (text or "").lower()
    return any(cue in t for cue in NEG_CUES)


POLISH_RULES = """You are converting a medication dosage schedule into one short
English sentence. ABSOLUTE RULES:
1. Every number in your sentence MUST appear in the schedule string. Never
   invent, round, or convert doses or times.
2. Name only the given drug. No other medications, indications, or facts.
3. If the schedule is unclear, output exactly: UNCLEAR
Output ONLY JSON: {"phrase": "<sentence>"}"""

CODEX_MODEL = "gpt-5.6-luna"


def llm_polish_schedule(pid: str, med_id: str, term: str, schedule: str,
                        cache_path: Path) -> str | None:
    """Ask Codex CLI to phrase a dosage schedule; validate before accepting.

    Validation: every number in the phrase must exist in the source schedule,
    and the drug term (or its first word) should appear. Returns None on any
    failure - caller then keeps the raw schedule text.
    """
    key_obj = {"pid": pid, "med": med_id, "term": term, "schedule": schedule}
    key = f"polish|{hashlib.sha256(json.dumps(key_obj, sort_keys=True).encode()).hexdigest()[:16]}"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    if key in cache:
        return cache[key]

    prompt = (f"{POLISH_RULES}\n\nDrug: {term}\n"
              f"Schedule: {schedule}\n\nJSON output:")
    result = None
    try:
        run = subprocess.run(
            ["codex", "exec", "-m", CODEX_MODEL, "--skip-git-repo-check",
             "-c", "model_reasoning_effort=low", "-"],
            input=prompt, capture_output=True, text=True, timeout=600)
        match = re.search(r'\{\s*"phrase".*?\}', run.stdout, re.DOTALL)
        if run.returncode == 0 and match:
            phrase = json.loads(match.group(0)).get("phrase", "").strip()
            sched_numbers = set(re.findall(r"\d+(?:\.\d+)?", schedule))
            phrase_numbers = set(re.findall(r"\d+(?:\.\d+)?", phrase))
            grounded = phrase_numbers <= sched_numbers
            mentions_drug = term.split()[0].lower() in phrase.lower()
            if phrase and phrase != "UNCLEAR" and grounded and mentions_drug \
                    and len(phrase) < 200:
                result = phrase
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: polish failed for {pid}/{med_id}: {exc}", file=sys.stderr)

    cache[key] = result
    cache_path.write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n")
    return result


def age_from(birth_date: str) -> int | None:
    if not birth_date:
        return None
    try:
        b = datetime.strptime(birth_date[:10], "%Y-%m-%d")
        today = datetime.now(timezone.utc)
        return today.year - b.year - ((today.month, today.day) < (b.month, b.day))
    except ValueError:
        return None


def collect_facts(bundle: dict, lookup: dict) -> dict:
    facts: dict = {"demographics": {}, "conditions": [], "observations": [],
                   "procedures": [], "medications": [], "unresolved_terms": []}
    for entry in bundle.get("entry", []):
        r = entry["resource"]
        rtype = r.get("resourceType")
        if rtype == "Patient":
            facts["demographics"] = {"age": age_from(r.get("birthDate", "")),
                                     "sex": r.get("gender")}
        elif rtype == "Condition":
            term = term_for(r, lookup)
            text = json.dumps(r.get("code", {}), ensure_ascii=False) + \
                   json.dumps(r.get("note", ""), ensure_ascii=False)
            if term is None:
                facts["unresolved_terms"].append(r.get("id"))
            else:
                facts["conditions"].append({"term": term, "negated": is_negated(text)})
        elif rtype == "Observation":
            term = term_for(r, lookup)
            if term is None:
                facts["unresolved_terms"].append(r.get("id"))
                continue
            value = r.get("valueQuantity", {})
            val = value.get("value")
            unit = value.get("unit") or value.get("code")
            interp = json.dumps(r.get("interpretation", ""), ensure_ascii=False)
            negated = is_negated(interp) or is_negated(term or "")
            facts["observations"].append({
                "term": term, "value": val, "unit": unit,
                "negated": negated,
            })
        elif rtype == "Procedure":
            term = term_for(r, lookup)
            if term is None:
                facts["unresolved_terms"].append(r.get("id"))
                continue
            facts["procedures"].append({"term": term})
        elif rtype == "MedicationAdministration":
            med_ref = (r.get("medicationReference") or {}).get("reference", "")
            term = None
            for e2 in bundle.get("entry", []):
                m = e2["resource"]
                if f"{m.get('resourceType')}/{m.get('id')}" == med_ref:
                    term = term_for(m, lookup)
            if term is None:
                facts["unresolved_terms"].append(r.get("id"))
                continue
            status = r.get("status", "")
            d = r.get("dosage") or {}
            if isinstance(d, list):
                d = d[0] if d else {}
            schedule = d.get("text", "") if isinstance(d, dict) else ""
            facts["medications"].append({
                "term": term,
                "prior": status in ("completed", "stopped"),
                "negated": is_negated(status),
                "schedule": schedule,
            })
    return facts


def render(facts: dict, polished: dict[str, str] | None = None,
           negation_mode: str = "omit") -> tuple[str, list[str]]:
    polished = polished or {}
    sanctioned: list[str] = []
    parts = []
    d = facts["demographics"]
    if d.get("age") and d.get("sex"):
        parts.append(f"Patient is a {d['age']}-year-old {d['sex']}")
    conds = [c["term"] for c in facts["conditions"] if not c["negated"]]
    if conds:
        parts[-1] += " with a history of " + ", ".join(conds) + "."
    elif parts:
        parts[-1] += "."
    obs = [(o["term"], o["value"], o["unit"]) for o in facts["observations"]
           if not o["negated"]]
    if obs:
        rendered = []
        for term, val, unit in obs:
            rendered.append(f"{term} ({val} {unit})" if val is not None else term)
        parts.append("Laboratory results included " + "; ".join(rendered) + ".")
    meds_prior = sorted({m["term"] for m in facts["medications"] if m["prior"]})
    meds_now = sorted({m["term"] for m in facts["medications"] if not m["prior"]})
    if meds_now:
        # polished phrases may end with their own period; strip to avoid ".."
        phrases = [polished.get(t, t).rstrip(".") for t in meds_now]
        parts.append("Current medications include " + ", ".join(phrases) + ".")
    if meds_prior:
        parts.append("Prior treatment included " + ", ".join(meds_prior) + ".")
    procs = [p["term"] for p in facts["procedures"]]
    if procs:
        parts.append("Procedures included " + ", ".join(procs) + ".")
    negs = [f for group in ("conditions", "observations")
            for f in facts[group] if f["negated"]]
    if negs and negation_mode == "state":
        phrase = "; ".join(f"{n['term']}: not detected" for n in negs)
        sentence = f"Notable negatives: {phrase}."
        parts.append(sentence)
        sanctioned.append(sentence)
    return " ".join(parts), sanctioned


def gate(note: str, facts: dict, sanctioned: list[str] | None = None) -> dict:
    sanctioned = sanctioned or []
    sanctioned_text = " ".join(sanctioned)
    note_numbers = set(re.findall(r"\d+(?:\.\d+)?", note))
    fact_numbers = set()

    def add_number_strings(v):
        if isinstance(v, str):
            fact_numbers.update(re.findall(r"\d+(?:\.\d+)?", v))

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("value", "age") and isinstance(v, (int, float)):
                    fact_numbers.add(str(v))
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(facts)
    # digits inside approved terminology (COVID-19, SARS-CoV-2, "list 3") are
    # part of the term itself, not invented clinical values
    for group in ("conditions", "observations", "procedures", "medications"):
        for f in facts[group]:
            add_number_strings(f["term"])
            if "schedule" in f:
                add_number_strings(f["schedule"])
    add_number_strings(str(facts["demographics"].get("sex", "")))
    invented_numbers = sorted(n for n in note_numbers if n not in fact_numbers)
    negated_used = [f["term"] for group in ("conditions", "observations")
                    for f in facts[group]
                    if f["negated"] and f["term"] in note
                    and not (sanctioned_text and f["term"] in sanctioned_text
                             and any(s in note for s in sanctioned))]
    dropped = [{"term": f["term"], "reason": "negated"}
               for group in ("conditions", "observations")
               for f in facts[group] if f["negated"]]
    unresolved = facts.get("unresolved_terms", [])
    return {
        "invented_numbers": invented_numbers,
        "negation_flips": negated_used,
        "dropped_facts": dropped,
        "unresolved_terms": unresolved,
        "hard_fail": bool(invented_numbers or negated_used or unresolved),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--patients-dir", default="out/patients")
    ap.add_argument("--glossary", default="config/glossary_stage4.json")
    ap.add_argument("--out", default="out")
    ap.add_argument("--llm-polish", action="store_true",
                    help="route dosage schedules through Codex CLI for phrasing "
                         "(validated against source numbers; off by default)")
    ap.add_argument("--negation-mode", choices=["omit", "state"], default="omit",
                    help="omit: negated facts left out of prose (retrieval-safe). "
                         "state: rendered explicitly as '<term>: not detected'")
    args = ap.parse_args()

    patients_dir = Path(args.patients_dir)
    notes_dir = Path(args.out) / "notes"
    rejected_dir = Path(args.out) / "rejected"
    notes_dir.mkdir(parents=True, exist_ok=True)
    rejected_dir.mkdir(parents=True, exist_ok=True)

    lookup, unapproved = load_glossary(Path(args.glossary))
    report = {"ran_at": datetime.now(timezone.utc).isoformat(),
              "glossary_sha256": sha256_file(Path(args.glossary)),
              "unapproved_glossary_entries_skipped": len(unapproved),
              "patients": {}}

    bundles = sorted(patients_dir.glob("*.fhir.json"))
    polish_cache = Path(args.out) / "llm_polish_cache.json"
    facts_dir = Path(args.out) / "facts"
    facts_dir.mkdir(parents=True, exist_ok=True)
    for bf in bundles:
        bundle = json.loads(bf.read_text(encoding="utf-8"))
        pid = bf.name.replace(".fhir.json", "")
        facts = collect_facts(bundle, lookup)
        # Negated facts are omitted from the note (retrieval safety: dense
        # models ignore negation) but preserved here - downstream eligibility
        # reasoning needs them to satisfy "no history of X" criteria.
        (facts_dir / f"{pid}.facts.json").write_text(
            json.dumps({"patient_id": pid, "facts": facts,
                        "negation_cues": NEG_CUES},
                       indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        polished: dict[str, str] = {}
        if args.llm_polish:
            for i, m in enumerate(facts["medications"]):
                sched = m.get("schedule", "")
                if not sched or m["negated"]:
                    continue
                phrase = llm_polish_schedule(pid, f"med-{i}", m["term"], sched,
                                             polish_cache)
                if phrase:
                    polished[m["term"]] = phrase
        note, sanctioned = render(facts, polished, args.negation_mode)
        verdict = gate(note, facts, sanctioned)
        dest = notes_dir if not verdict["hard_fail"] else rejected_dir
        dest.mkdir(parents=True, exist_ok=True)
        (dest / f"{pid}.txt").write_text(note + "\n", encoding="utf-8")
        # Per-note provenance: chains the note to the exact bundle bytes,
        # the split manifest that produced it, the source FHIR files behind
        # that manifest, and every resource ID inside - so any matched trial
        # can be traced back to its originating records.
        resource_ids = [f"{r.get('resourceType')}/{r.get('id')}"
                        for r in (e["resource"] for e in bundle.get("entry", []))]
        manifest = json.loads((Path(args.out) / "split_manifest.json").read_text()) \
            if (Path(args.out) / "split_manifest.json").exists() else {}
        patient_manifest = manifest.get("patients", {}).get(pid, {})
        provenance = {
            "patient_id": pid,
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "note_file": str(dest / f"{pid}.txt"),
            "note_sha256": hashlib.sha256(note.encode()).hexdigest(),
            "negation_mode": args.negation_mode,
            "llm_polish": bool(args.llm_polish),
            "glossary": {"file": args.glossary,
                         "sha256": sha256_file(Path(args.glossary))},
            "source_bundle": {
                "file": str(bf),
                "sha256": sha256_file(bf),
                "resource_ids": sorted(resource_ids),
                "resource_hashes": patient_manifest.get("resource_hashes", {}),
            },
            "split_manifest": {
                "file": str(Path(args.out) / "split_manifest.json"),
                "input_files_with_hashes": manifest.get("provenance", {}).get("files", {}),
            },
        }
        (dest / f"{pid}.provenance.json").write_text(
            json.dumps(provenance, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        report["patients"][pid] = {
            "verdict": "PASS" if not verdict["hard_fail"] else "FAIL",
            "note_file": str(dest / f"{pid}.txt"),
            "provenance_file": str(dest / f"{pid}.provenance.json"),
            **verdict,
        }

    (Path(args.out) / "gate_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8")
    n_pass = sum(1 for p in report["patients"].values() if p["verdict"] == "PASS")
    print(f"Notes: {n_pass}/{len(bundles)} passed the gate "
          f"({notes_dir}); failures in {rejected_dir}")
    print("Gate report:", Path(args.out) / "gate_report.json")


if __name__ == "__main__":
    main()
