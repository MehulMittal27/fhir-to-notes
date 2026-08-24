"""Routing of FHIR code systems to translation tiers.

The system URI is the discriminator: it names which terminology a code belongs
to, which decides how its English term is obtained. Routing is data so new
systems are one-line additions.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TierRule:
    match: tuple[str, ...]
    tier: str

TIER_RULES: list[TierRule] = [
    TierRule(match=("icd-10-gm", "icd-10"), tier="code_lookup"),
    TierRule(match=("atc",), tier="code_lookup"),
    TierRule(match=("loinc.org", "loinc", "snomed.info/sct", "/sct"), tier="english_native"),
    TierRule(match=("ops",), tier="llm_review"),
    TierRule(match=("uk-essen.de",), tier="llm_review"),
]

FALLBACK_TIER = "llm_review"


def classify(system: str) -> str:
    s = (system or "").lower()
    for rule in TIER_RULES:
        if any(m in s for m in rule.match):
            return rule.tier
    return FALLBACK_TIER
