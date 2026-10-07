# AGENTS.md - conventions for agents working in this repo

Read this before writing or modifying any code here.

## Prime directive

This repository processes clinical-shaped data that the owner has marked
confidential. NEVER transmit data, derived artifacts, glossary contents, or
code snippets containing real values to any external service, API, or
repository. All processing is local by default.

Sole documented network exceptions (read-only GETs, bare codes/names only -
never patient data):
  - `src/term_lookup.py` -> NLM Clinical Tables: ICD code -> English name.
    Cached permanently in `out/term_lookup_cache.json`; failures not cached.
  - `src/term_lookup.py verify_drug_name()` + stage2 -> NLM RxNorm: verify an
    LLM-proposed English drug name and adopt RxNorm's canonical spelling.
Any additional network touchpoint requires updating this file and README first.

The LLM conversion path uses the owner's local Codex CLI installation
(model recorded per run in output provenance). No records leave the machine
beyond what the user explicitly runs through it.

## Stage architecture

Each stage is one script in `src/`, numbered. Rules:

- Stages communicate ONLY through JSON files in `out/`. No stage imports another.
- Every stage is idempotent: running it twice produces byte-identical outputs.
- Every stage writes a provenance block into its output:
  `{input_paths, input_sha256s, script_sha256, ran_at, config}`.
- Every stage accepts `--input` and `--out`; no hardcoded absolute paths.
- Scalability contract: new FHIR files dropped into the input directory must be
  picked up without code changes. Unknown resource types and unknown code
  systems are recorded, never fatal.

## Code conventions

- Python 3.11+, stdlib only for stages 1-3 (json, argparse, hashlib, pathlib).
- No comments explaining what; docstrings explain why where intent is subtle.
- Fail loudly: malformed resources emit warnings and are skipped with a count;
  silent data loss is forbidden.
- Deterministic ordering everywhere: sort outputs; never iterate sets directly.

## Terminology tiers (glossary routing)

| system URI contains | tier | route |
|---|---|---|
| `icd-10-gm`, `atc` | 1 | deterministic reference lookup |
| `loinc.org`, `snomed` (`sct`) | native | English display copied as-is |
| anything else (hospital-internal, OPS) | 2 | LLM proposal + grounding validator + human review |

The ICD lookup hits US ICD-10-CM, not GM: only an exact code match may be
`auto`; nearest/prefix matches and GM-only codes are `pending_review`.

Routing lives in `src/tiers.py` as data, not inline conditionals, so adding a
new system is a one-line change.

## The faithfulness gate (stage 4) is law

Negation flips, invented facts, and invented numbers are fatal violations.
Only structured FHIR negation (Condition.verificationStatus refuted) makes a
fact negated; a text cue alone makes it uncertain, never absent.
A note with any fatal violation does not ship, regardless of how good it reads.
Dropped facts are counted per note in `out/gate_report.json`.

## Tests

`python3 -m unittest discover -s tests`. Synthetic fixtures only (made-up
codes and records); mock NLM and the LLM - no network, never read `out/` or
`config/glossary*.json`.

## Decision log

Material decisions are appended to `docs/decisions.md` with date and rationale.
Do not rewrite past entries.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
