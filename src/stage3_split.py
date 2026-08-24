"""Stage 3: FHIR Split.

Emit one self-contained FHIR collection bundle per patient from the same input
files Stage 1 scanned. Referenced definitional resources (e.g. the Medication
behind a MedicationAdministration) are copied into the owning patient's bundle
so each bundle is truly self-contained. Consumed by TrialMatchAI:
trialmatchai import-patient --format fhir.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fhirutil import (  # noqa: E402
    DEFINITION_TYPES, index_definitions, iter_resources, referenced_definition_keys,
    sha256_file, subject_of,
)


def canonical_sha256(resource: dict) -> str:
    """Content hash of one resource; survives serialization differences
    (whitespace, key order) but catches any content change."""
    payload = json.dumps(resource, sort_keys=True, ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", default="out")
    args = ap.parse_args()

    input_dir = Path(args.input).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()
    patients_dir = out_dir / "patients"
    patients_dir.mkdir(parents=True, exist_ok=True)

    all_resources = []
    for path in sorted(input_dir.rglob("*.json")):
        if path.name == ".DS_Store":
            continue
        all_resources.extend(res for _, res in iter_resources(path))

    definitions = index_definitions(res for res in all_resources
                                    if res.get("resourceType") in DEFINITION_TYPES)

    per_patient: dict[str, list[dict]] = {}
    for res in all_resources:
        rtype = res.get("resourceType", "Unknown")
        pid = subject_of(res)
        if rtype in DEFINITION_TYPES or rtype == "Unknown":
            continue
        if pid is None:
            continue
        bundle_resources = [res]
        for def_key in referenced_definition_keys(res, definitions):
            bundle_resources.append(definitions[def_key])
        per_patient.setdefault(pid, []).extend(bundle_resources)

    manifest = {"provenance": {
        "input_dir": str(input_dir),
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "files": {str(p.relative_to(input_dir)): sha256_file(p)
                  for p in sorted(input_dir.rglob("*.json")) if p.name != ".DS_Store"},
    }, "patients": {}}

    for pid in sorted(per_patient):
        unique, ids = [], set()
        for r in per_patient[pid]:
            key = (r.get("resourceType"), r.get("id"))
            if key not in ids:
                ids.add(key)
                unique.append(r)
        per_patient[pid] = unique

        bundle = {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [{"resource": r} for r in sorted(
                unique, key=lambda r: (r.get("resourceType", ""), r.get("id", "")))],
        }
        out_file = patients_dir / f"{pid}.fhir.json"
        out_file.write_text(json.dumps(bundle, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
        types: dict[str, int] = {}
        resource_hashes: dict[str, str] = {}
        for r in unique:
            types[r.get("resourceType")] = types.get(r.get("resourceType"), 0) + 1
            resource_hashes[f"{r.get('resourceType')}/{r.get('id')}"] = \
                canonical_sha256(r)
        manifest["patients"][pid] = {"file": str(out_file),
                                     "resources": len(unique),
                                     "types": dict(sorted(types.items())),
                                     "resource_hashes": resource_hashes}

    (out_dir / "split_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"Wrote {len(per_patient)} patient bundles to {patients_dir}")


if __name__ == "__main__":
    main()
