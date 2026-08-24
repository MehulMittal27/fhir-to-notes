"""Stage 1: Inventory scan.

Walks a directory of FHIR JSON files (bundles, single resources, or arrays),
groups resources by patient, and inventories every unique code system triple.
Definitional resources (Medication etc.) carry no subject themselves but are
joined into the inventory through references from patient-care resources, so
drug names reach the glossary.

Idempotent: outputs are sorted; identical inputs produce identical bytes.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fhirutil import (  # noqa: E402
    DEFINITION_TYPES, index_definitions, iter_resources, referenced_definition_keys,
    sha256_file, subject_of, walk_codings,
)
from tiers import classify  # noqa: E402


def sha256_obj(obj) -> str:
    payload = json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True, help="directory containing FHIR JSON files")
    ap.add_argument("--out", default="out", help="output directory")
    args = ap.parse_args()

    input_dir = Path(args.input).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(f for f in input_dir.rglob("*.json") if f.name != ".DS_Store")

    all_resources = []
    file_reports = []
    for path in files:
        n = 0
        for _, res in iter_resources(path):
            all_resources.append(res)
            n += 1
        file_reports.append({"file": str(path.relative_to(input_dir)),
                             "sha256": sha256_file(path), "resources": n})

    definitions = index_definitions(res for res in all_resources
                                    if res.get("resourceType") in DEFINITION_TYPES)

    patients: dict[str, dict] = {}
    concepts: dict[tuple, dict] = {}
    type_counts: defaultdict = defaultdict(int)
    skipped_orphans = 0

    def attribute(pid: str, rtype: str) -> None:
        p = patients.setdefault(pid, {"id": pid, "gender": None, "birthDate": None,
                                      "source_file": None, "resource_types": {}})
        p["resource_types"][rtype] = p["resource_types"].get(rtype, 0) + 1

    def add_coding(c: dict, pid: str | None, rtype: str) -> None:
        key = (c.get("system", ""), c.get("code", ""), c.get("display", ""))
        concept = concepts.setdefault(key, {
            "system": key[0], "code": key[1], "display": key[2],
            "tier": classify(key[0]), "resource_types": set(), "patients": set(),
        })
        concept["resource_types"].add(rtype)
        if pid:
            concept["patients"].add(pid)

    for res in all_resources:
        rtype = res.get("resourceType", "Unknown")
        type_counts[rtype] += 1
        pid = subject_of(res)

        if rtype == "Patient":
            p = patients.setdefault(pid, {"id": pid, "gender": None, "birthDate": None,
                                          "source_file": None, "resource_types": {}})
            p.update(gender=res.get("gender"), birthDate=res.get("birthDate"))
            continue
        if rtype in DEFINITION_TYPES:
            continue
        if pid is None:
            skipped_orphans += 1
            continue
        attribute(pid, rtype)

        codings: list = []
        walk_codings(res, codings)
        for c in codings:
            add_coding(c, pid, rtype)

        for def_key in referenced_definition_keys(res, definitions):
            definition = definitions[def_key]
            attribute(pid, f"Definition:{definition['resourceType']}")
            def_codings: list = []
            walk_codings(definition, def_codings)
            for c in def_codings:
                add_coding(c, pid, definition["resourceType"])

    tier_of_concept: defaultdict = defaultdict(set)
    concept_list = []
    for key, c in sorted(concepts.items(),
                         key=lambda kv: (kv[1]["tier"], kv[0])):
        concept_list.append({
            "system": c["system"], "code": c["code"], "display": c["display"],
            "tier": c["tier"],
            "resource_types": sorted(c["resource_types"]),
            "patient_count": len(c["patients"]),
        })
        tier_of_concept[c["tier"]].add(key)

    inventory = {
        "provenance": {
            "input_dir": str(input_dir),
            "files": file_reports,
            "ran_at": datetime.now(timezone.utc).isoformat(),
        },
        "totals": {
            "files": len(files),
            "resources": sum(type_counts.values()),
            "resource_types": dict(sorted(type_counts.items())),
            "patients": len(patients),
            "unique_concepts": len(concept_list),
            "concepts_by_tier": {t: len(v) for t, v in sorted(tier_of_concept.items())},
            "orphan_resources_without_patient": skipped_orphans,
        },
        "concepts": concept_list,
    }
    out_path = out_dir / "inventory.json"
    out_path.write_text(json.dumps(inventory, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    t = inventory["totals"]
    print(f"Inventory written: {out_path}")
    print(f"  files={t['files']}  resources={t['resources']}  "
          f"patients={t['patients']}  unique_concepts={t['unique_concepts']}")
    print(f"  concepts by tier: {t['concepts_by_tier']}")
    if skipped_orphans:
        print(f"  WARN: {skipped_orphans} resources had no patient reference")


if __name__ == "__main__":
    main()
