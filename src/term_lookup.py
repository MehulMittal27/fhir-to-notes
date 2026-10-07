"""External terminology resolution and drug-name verification.

Documented network exception per AGENTS.md: read-only HTTPS GET requests to
  - clinicaltables.nlm.nih.gov (ICD codes -> English names)
  - rxnav.nlm.nih.gov (RxNorm: verify a proposed English drug name exists and
    fetch its canonical spelling; bare names only, never patient data)

Successful resolutions are cached permanently in out/term_lookup_cache.json;
a query is fetched at most once ever. Failures are NOT cached, so a transient
outage never becomes a permanently unresolvable term. On failure, resolution
returns None and callers fall back to lower tiers - never fatal.

ICD lookups query the US ICD-10-CM table while the corpus is coded in German
ICD-10-GM. Only an exact code match is the same concept. Any other hit is a
nearby CM code whose meaning can differ (laterality, site, subtype), so it is
returned with match="prefix" and a review_reason; callers must route it to
human review, never auto.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ICD_ENDPOINT = "https://clinicaltables.nlm.nih.gov/api/icd10cm/v3/search"
RXNORM_BY_NAME = "https://rxnav.nlm.nih.gov/REST/rxcui.json?name="
RXNORM_PROPS = "https://rxnav.nlm.nih.gov/REST/rxcui/{}/properties.json"


def resolve(system: str, code: str, cache_path: Path, timeout: int = 15) -> dict | None:
    cache = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())
    key = f"{system}|{code}"
    if key in cache:
        hit = cache[key]
        return _classify_icd(code, hit) if _system_key(system) == "icd-10-gm" else hit

    kind = _system_key(system)
    if kind == "atc":
        # ATC codes have no public resolution API; they are resolved via the
        # LLM-proposal + RxNorm-verification path in stage2, not here.
        return None
    result = _resolve_icd(code, timeout)

    if result is not None:
        cache[key] = result
        cache_path.write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n")
    return result


def verify_drug_name(name: str, cache_path: Path, timeout: int = 15) -> dict | None:
    """Verify an English drug name against NLM RxNorm.

    Returns {en, rxcui, source} when RxNorm knows the exact name, else None.
    Canonical spelling comes from RxNorm itself, not the proposer.
    """
    key = f"rxnorm|{name.lower()}"
    cache = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())
    if key in cache:
        return cache[key]

    result = None
    try:
        url = RXNORM_BY_NAME + urllib.parse.quote(name)
        data = _get_json(url, timeout)
        ids = (data or {}).get("idGroup", {}).get("rxnormId", [])
        if ids:
            props = _get_json(RXNORM_PROPS.format(ids[0]), timeout)
            canonical = ((props or {}).get("properties") or {}).get("name")
            if canonical:
                result = {"en": canonical, "rxcui": ids[0],
                          "source": f"NLM RxNorm (rxcui {ids[0]})"}
    except Exception as exc:  # noqa: BLE001 - offline must never crash a stage
        print(f"WARN: RxNorm verification failed ({name}): {exc}", file=sys.stderr)

    if result is not None:
        cache[key] = result
        cache_path.write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n")
    return result


def _system_key(system: str) -> str:
    s = (system or "").lower()
    if "icd-10" in s:
        return "icd-10-gm"
    return s


def _resolve_icd(code: str, timeout: int) -> dict | None:
    url = ICD_ENDPOINT + "?" + urllib.parse.urlencode(
        {"sf": "code,name", "terms": code, "max_results": "5"})
    data = _get_json(url, timeout)
    if not data:
        return None
    rows = (data[3] if len(data) > 3 else []) or []
    exact = [r for r in rows if r[0].upper() == code.upper()]
    if exact:
        return _classify_icd(code, {"en": exact[0][1], "matched_code": exact[0][0],
                                    "source": "NLM Clinical Tables (ICD-10-CM)"})
    prefix = [r for r in rows if r[0].upper().startswith(code.upper())]
    if prefix:
        return _classify_icd(code, {
            "en": prefix[0][1], "matched_code": prefix[0][0],
            "cm_candidates": sorted(r[0] for r in prefix),
            "source": f"NLM Clinical Tables (ICD-10-CM {prefix[0][0]}, nearest match for {code})"})
    return None


def _classify_icd(code: str, result: dict) -> dict:
    """Stamp match kind and review reason onto an ICD result.

    Also applied to cache hits, so entries cached before match classification
    existed (prefix hits stored without a marker) are classified the same way.
    """
    matched = result.get("matched_code") or ""
    if matched.upper() == code.upper():
        return {**result, "match": "exact"}
    candidates = result.get("cm_candidates") or [matched]
    return {**result, "match": "prefix", "cm_candidates": candidates,
            "review_reason": (
                f"ICD-10-GM {code} has no exact ICD-10-CM entry; proposed CM "
                f"{matched} is a more specific code whose meaning may differ "
                f"(CM candidates: {', '.join(candidates)}) - needs human review")}


def _get_json(url: str, timeout: int, accept: str = "application/json"):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "fhir-to-notes/0.1",
                                                   "Accept": accept})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 - offline must never crash a stage
        print(f"WARN: terminology lookup failed ({url.split('?')[0]}): {exc}",
              file=sys.stderr)
        return None
