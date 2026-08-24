"""Shared FHIR traversal helpers for all stages."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

DEFINITION_TYPES = {"Location", "Medication", "Organization", "Practitioner"}
REFERENCE_FIELDS = ("medicationReference", "subject", "patient", "encounter",
                    "location")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def iter_resources(path: Path):
    """Yield (file, resource_dict) for bundles, single resources, and arrays."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        print(f"WARN: not valid JSON, skipping: {path}", file=sys.stderr)
        return
    if isinstance(data, dict) and data.get("resourceType") == "Bundle":
        for entry in data.get("entry", []):
            res = entry.get("resource")
            if isinstance(res, dict):
                yield path, res
    elif isinstance(data, dict) and "resourceType" in data:
        yield path, data
    elif isinstance(data, list):
        for res in data:
            if isinstance(res, dict) and "resourceType" in res:
                yield path, res
    else:
        print(f"WARN: no FHIR resource found, skipping: {path}", file=sys.stderr)


def subject_of(resource: dict) -> str | None:
    ref = (resource.get("subject") or {}).get("reference") \
        or (resource.get("patient") or {}).get("reference")
    if ref:
        return ref.split("/")[-1]
    if resource.get("resourceType") == "Patient":
        return resource.get("id")
    return None


def walk_codings(node, out: list):
    if isinstance(node, dict):
        for c in node.get("coding", []):
            if isinstance(c, dict):
                out.append(c)
        for v in node.values():
            walk_codings(v, out)
    elif isinstance(node, list):
        for v in node:
            walk_codings(v, out)


def index_definitions(resources) -> dict[str, dict]:
    """Key definitional resources by 'ResourceType/id' so references resolve."""
    defs: dict[str, dict] = {}
    for res in resources:
        rtype = res.get("resourceType")
        if rtype in DEFINITION_TYPES and res.get("id"):
            defs[f"{rtype}/{res['id']}"] = res
    return defs


def referenced_definition_keys(resource: dict, known: dict[str, dict]) -> list[str]:
    """Return keys from `known` that this resource references anywhere."""
    hits = []
    def walk(node):
        if isinstance(node, dict):
            for field, value in node.items():
                ref = value.get("reference") if isinstance(value, dict) else None
                if field in REFERENCE_FIELDS and isinstance(ref, str) and ref in known:
                    hits.append(ref)
                else:
                    walk(value)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(resource)
    return sorted(set(hits))
