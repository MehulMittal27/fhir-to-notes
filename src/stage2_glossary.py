"""Stage 2: Glossary.

Resolve every unique concept from inventory.json to an English term.

Tiers:
  english_native -> display copied as-is (status: auto)
  code_lookup    -> local reference table config/icd10_atc_en.json (status: auto);
                    then NLM ICD-10-CM: exact match auto, nearest (prefix) match
                    pending_review with the proposed CM code and review_reason;
                    no match -> LLM proposal, pending_review
  llm_review     -> Codex CLI proposal, grounded, status: pending_review

Outputs:
  out/glossary_proposals.json  full machine-readable detail incl. LLM raw output
  config/glossary.json         the working glossary; human flips pending_review
                               entries to approved after review
Idempotent: LLM calls are cached in out/glossary_llm_cache.json; reruns only
call the model for concepts not already cached.
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
from term_lookup import resolve as resolve_external, verify_drug_name  # noqa: E402

CODEX_MODEL = "gpt-5.6-luna"

REVIEW_FIELDS = ("matched_code", "match", "cm_candidates", "review_reason")
ICD_UNRESOLVED_REASON = ("no exact ICD-10-CM entry for this ICD-10-GM code "
                         "(German-only code, or lookup unavailable) - needs human review")

LLM_SYSTEM_RULES = """You are a medical terminology translator. For each entry,
produce the standard English medical term for the German display string.
Rules:
1. Literal medical equivalence only. No elaboration, no added details.
2. Expand common German medical abbreviations to full English terms
   (e.g. "Haut" skin, "Hämatokrit" hematocrit).
3. If you are NOT certain what a term or abbreviation means, keep it verbatim
   and set confidence to "low".
4. Output ONLY a JSON array: [{"id": "<id>", "en": "<english term>",
   "confidence": "high"|"low"}] with one object per input id."""


def sha256_obj(obj) -> str:
    payload = json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def call_codex(entries: list[dict], model: str) -> str:
    payload = json.dumps(
        [{"id": e["id"], "code": e["code"], "display": e["de"]} for e in entries],
        ensure_ascii=False,
        indent=1,
    )
    prompt = f"{LLM_SYSTEM_RULES}\n\nENTRIES:\n{payload}"
    result = subprocess.run(
        ["codex", "exec", "-m", model, "--skip-git-repo-check",
         "-c", "model_reasoning_effort=low", "-"],
        input=prompt, capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"codex exec failed ({result.returncode}): {result.stderr[:500]}")
    return result.stdout


def extract_json_array(text: str) -> list | None:
    match = re.search(r"\[\s*\{.*\}\s*\]", text, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, list) else None
    except json.JSONDecodeError:
        return None


def is_unreviewed_prefix_match(entry: dict) -> bool:
    """An auto entry produced by ICD-10-CM prefix fallback before such matches
    were routed to review. Recognised by the match marker or, for glossaries
    written before the marker existed, by the 'nearest match for' source text."""
    if entry.get("status") != "auto":
        return False
    return entry.get("match") == "prefix" or \
        "nearest match for" in (entry.get("source") or "")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inventory", default="out/inventory.json")
    ap.add_argument("--out", default="out")
    ap.add_argument("--config", default="config")
    ap.add_argument("--model", default=CODEX_MODEL)
    args = ap.parse_args()

    out_dir = Path(args.out)
    cfg_dir = Path(args.config)
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir.mkdir(parents=True, exist_ok=True)

    inventory = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    reference = json.loads((cfg_dir / "icd10_atc_en.json").read_text(encoding="utf-8"))

    # Lookup-first: an existing glossary entry with a resolved English term is
    # carried forward untouched. No API calls, no LLM calls, ever, for terms we
    # have already settled. Only new/failed concepts flow into resolution.
    prior_path = cfg_dir / "glossary.json"
    prior = json.loads(prior_path.read_text())["terms"] if prior_path.exists() else {}

    cache_path = out_dir / "glossary_llm_cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    glossary: list[dict] = []
    needs_llm: list[dict] = []
    carried = 0
    rerouted = 0
    for c in inventory["concepts"]:
        cid = f"{c['system']}|{c['code']}|{c['display']}"
        entry = {
            "id": cid, "system": c["system"], "code": c["code"],
            "de": c["display"], "tier": c["tier"],
            "resource_types": c["resource_types"], "status": "pending_review",
            "en": None, "source": None,
        }
        prior_entry = prior.get(cid)
        if prior_entry and is_unreviewed_prefix_match(prior_entry):
            rerouted += 1
        elif prior_entry and prior_entry.get("en") and \
                prior_entry.get("status") in ("auto", "approved"):
            entry.update(en=prior_entry["en"], source=prior_entry.get("source"),
                         status=prior_entry["status"],
                         resolution=prior_entry.get("resolution", "carried_forward"))
            entry.update({k: prior_entry[k] for k in REVIEW_FIELDS if k in prior_entry})
            glossary.append(entry)
            carried += 1
            continue
        if c["tier"] == "english_native" and c["display"].strip():
            entry.update(en=c["display"].strip(), source="native English display",
                         status="auto")
        elif c["tier"] == "code_lookup":
            system_key = next((k for k in reference if k in c["system"]), None)
            ref = reference.get(system_key, {}).get(c["code"]) if system_key else None
            if ref:
                entry.update(en=ref["en"], source=ref["source"], status="auto")
                entry["resolution"] = "local_reference"
            else:
                ext = resolve_external(c["system"], c["code"],
                                       cache_path=out_dir / "term_lookup_cache.json")
                if ext:
                    # Only an exact ICD-10-CM code match is the same concept as
                    # the ICD-10-GM code. A prefix (nearest) match is a different,
                    # more specific CM code, so it is a proposal for review.
                    needs_approval = ext.get("match") != "exact"
                    entry.update(en=ext["en"], source=ext["source"],
                                 status="pending_review" if needs_approval else "auto",
                                 resolution="nlm_api")
                    entry.update({k: ext[k] for k in REVIEW_FIELDS if k in ext})
                else:
                    entry.update(
                        tier="llm_review",
                        source="no local or external resolution - demoted to LLM review")
                    if "icd-10" in (c["system"] or "").lower():
                        entry["review_reason"] = ICD_UNRESOLVED_REASON
        if entry["en"] is None:
            needs_llm.append(entry)
        glossary.append(entry)

    uncached = [e for e in needs_llm if e["id"] not in cache]
    llm_raw = None
    if uncached:
        print(f"LLM tier: proposing {len(uncached)} terms via {args.model} ...")
        llm_raw = call_codex(uncached, args.model)
        proposals = extract_json_array(llm_raw) or []
        for p in proposals:
            cache[p["id"]] = p
        cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n")

    for e in needs_llm:
        proposal = cache.get(e["id"])
        if proposal and proposal.get("en"):
            e.update(en=proposal["en"].strip(),
                     confidence=proposal.get("confidence", "low"),
                     source=f"LLM proposal ({args.model}) - REQUIRES HUMAN APPROVAL")
            # Drug terms get an upgrade path: verify the proposed INN against
            # NLM RxNorm. A verified name is authoritative and ships auto.
            if "atc" in (e["system"] or "").lower():
                verified = verify_drug_name(e["en"], cache_path=out_dir / "term_lookup_cache.json")
                if verified:
                    e.update(en=verified["en"], status="auto",
                             rxcui=verified["rxcui"],
                             source=f"{verified['source']} - verified LLM proposal")
        else:
            e["source"] = "LLM produced no usable proposal - resolve manually"

    proposals_doc = {
        "provenance": {
            "model": args.model,
            "llm_called": bool(uncached),
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "rules": LLM_SYSTEM_RULES,
            "raw_output": llm_raw,
            "cache_sha256": sha256_obj(cache),
        },
        "glossary": sorted(glossary, key=lambda x: (x["status"], x["tier"], x["id"])),
    }
    (out_dir / "glossary_proposals.json").write_text(
        json.dumps(proposals_doc, indent=2, ensure_ascii=False) + "\n")

    draft = {
        "_comment": "Human review: flip status pending_review -> approved. "
                    "Only approved/auto entries are consumed by stage 4.",
        "provenance": proposals_doc["provenance"],
        "terms": {e["id"]: {k: e[k] for k in ("en", "status", "source", *REVIEW_FIELDS)
                            if k in e}
                  for e in sorted(glossary, key=lambda x: x["id"])},
    }
    (cfg_dir / "glossary.json").write_text(
        json.dumps(draft, indent=2, ensure_ascii=False) + "\n")

    n_auto = sum(1 for e in glossary if e["status"] == "auto")
    print(f"Glossary written: {len(glossary)} terms "
          f"({n_auto} auto, {len(glossary) - n_auto} pending your review; "
          f"{carried} carried forward from lookup - zero API/LLM calls for those)")
    if rerouted:
        print(f"Re-routed {rerouted} previously auto ICD-10-CM nearest-match terms "
              f"to pending_review (exact CM match required for auto)")
    print(f"Review file: {cfg_dir / 'glossary.json'}")


if __name__ == "__main__":
    main()
