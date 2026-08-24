# Decision Log

## 2026-08-24 - Option 1 (file-based) over live FHIR server
The conversion pipeline reads the JSON bundles directly from disk. The Docker
FHIR server in the source repo is a convenience layer, not a dependency: it can
be started later for interactive querying without affecting any stage here.

## 2026-08-24 - Prompting first, fine-tuning parked
Conversion uses an LLM under guards (grounding validator, negation rule engine,
deterministic renderer, faithfulness gate). LoRA training is deferred until the
verified pair corpus exceeds roughly 500 examples; at n=10 patients it would
only overfit.

## 2026-08-24 - Glossary-first German handling
German display strings are never translated inline. Concepts resolve through
tiers: deterministic code lookup where public terminologies exist (ICD-10-GM,
ATC), native English displays where present (LOINC, SNOMED), and LLM-assisted +
human-reviewed translation only for hospital-proprietary systems and OPS. The
glossary is built once per unique concept and reused forever - consistency by
construction.

## 2026-08-24 - Negation polarity is mechanical
Status/negation is decided by rule engines scanning evidence strings for cue
lists ("no", "denies", "without", "negative for", "ruled out"). The LLM may
propose polarity but cannot finalize it. Rationale: the canonical failure case
("no diabetes" rendered as diabetes) must be structurally impossible, not merely
unlikely.

## 2026-08-24 - Wikidata ATC resolutions are proposals, not autos
Live comparison against luna's proposals exposed a wrong community label
(N03AX14 -> "(S)-etiracetam" instead of Levetiracetam) and silent coverage gaps
(M09AA02, X99XT01 absent). Wikidata remains useful as a proposal source, but its
ATC answers enter the glossary as pending_review, same as LLM proposals. NLM
ICD-10-CM resolutions stay authoritative (status auto). Rationale: auto-status
is reserved for sources whose errors are rarer than human review cost.

## 2026-08-24 - Wikidata replaced by RxNorm verification for drug names
Wikidata's community ATC labels proved unreliable (wrong label N03AX14, silent
coverage gaps). Replaced with: LLM proposes the English INN, then stage2 verifies
the proposal against NLM RxNorm (rxcui lookup). Verified names adopt RxNorm's
canonical spelling and ship status auto; unverified proposals stay pending_review.
Rationale: resolution quality now rests on an authoritative NIH vocabulary rather
than a crowd-edited graph, while still scaling without per-code human effort for
common drugs.

## 2026-08-24 - Stage-4 working glossary with force-approved entries
config/glossary_stage4.json is a derived copy of config/glossary.json with every
status forced to "approved". Rationale: formal human review of the 25 LLM-tier
terms is still outstanding, but Stage 4 development cannot wait on it. Stage 4
consumes ONLY this file; the authoritative glossary keeps its true review states.
Risk accepted: until real review lands, generated notes may contain unreviewed
terminology - acceptable for development runs, NOT for published results.
Regenerate glossary_stage4.json from the authoritative file after review.

## 2026-08-24 - Lookup-first resolution order
Stage 2 now carries forward any concept already resolved in config/glossary.json
(status auto/approved) before consulting any external source. Full order:
1. existing glossary entry (carried forward, zero network/LLM)
2. local reference table (config/icd10_atc_en.json)
3. permanent per-query caches (term_lookup_cache.json, glossary_llm_cache.json)
4. NLM APIs (ICD lookup, RxNorm drug-name verification)
5. LLM proposal -> pending_review
Rationale: once a term is settled it must never cost an API call or LLM token
again, and repeated runs must be byte-stable.

## 2026-08-24 - DEFERRED: `f2n approve` command
When the human review of config/glossary.json is eventually completed, a small
`f2n approve` subcommand should regenerate config/glossary_stage4.json from it so
the Stage-4 working copy inherits all corrections automatically. Deliberately
deferred until that review happens; until then glossary_stage4.json remains the
force-approved working copy (see the 2026-08-24 force-approval decision).

## 2026-08-24 - Gate whitelists digits inside approved terminology
The faithfulness gate's invented-number check initially flagged digits that are
part of approved glossary terms (COVID-19 -> "19", SARS-CoV-2 -> "2", OPS
"display ... list 3" -> "3"). Allowed-number set now includes digits from all
approved term strings and demographic fields. Clinical values remain strictly
checked: any number in the note not traceable to source values or terms is fatal.

## 2026-08-24 - Per-resource canonical hashing
File-level seals (split manifest, stage4 provenance) could not localize changes:
editing one unrelated condition invalidated every note touching Condition.json.
Stage 3 now records sha256 over each resource's canonical JSON
(sort_keys, ensure_ascii=False) in split_manifest resource_hashes; stage4 stamps
the map into every note's provenance. Effect: edits elsewhere in a source file no
longer break a specific fact's chain; only a change to the cited resource itself
does. File-level hashes retained for whole-file change detection.

## 2026-08-24 - Unresolved terms are a gate failure
A concept missing from the working glossary (e.g. new terminology arriving with
new patients) previously caused a silent omission: the fact never reached the
note, yet the gate verdict stayed PASS. Now unresolved_terms is a hard gate
failure - the note lands in out/rejected/ until the term is approved through the
UI glossary (which can approve brand-new terms into the Stage-4 working copy).
Rationale: a note that looks complete but silently omits a diagnosis is worse
than a rejected note; silent data loss is forbidden by AGENTS.md.
