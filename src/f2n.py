#!/usr/bin/env python3
"""f2n - one entry point for the whole FHIR-to-notes pipeline.

Usage:
  python src/f2n.py run --input /path/to/fhir/examples
  python src/f2n.py stage1 --input ... [--out out]
  python src/f2n.py stage2 [--inventory out/inventory.json]
  python src/f2n.py stage3 --input ...
  python src/f2n.py status

`run` executes stages 1-3 in order (stage 4, note generation, pending).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).parent


def run_stage(script: str, args: list[str]) -> int:
    cmd = [sys.executable, str(SRC / script), *args]
    print(f"\n=== {' '.join(cmd)} ===")
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        print(f"STAGE FAILED: {script} exited {rc}", file=sys.stderr)
    return rc


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="execute stages 1-3 in order")
    p_run.add_argument("--input", required=True)
    p_run.add_argument("--out", default="out")
    p_run.add_argument("--config", default="config")
    p_run.add_argument("--negation-mode", choices=["omit", "state"], default="omit")
    p_run.add_argument("--llm-polish", action="store_true")

    for name in ("stage1", "stage2", "stage3"):
        p = sub.add_parser(name)
        p.add_argument("--input")
        p.add_argument("--out", default="out")
        p.add_argument("--config", default="config")
        p.add_argument("--inventory", default="out/inventory.json")

    p4 = sub.add_parser("stage4", help="guarded conversion: bundles -> notes")
    p4.add_argument("--patients-dir", default="out/patients")
    p4.add_argument("--glossary", default="config/glossary_stage4.json")
    p4.add_argument("--out", default="out")
    p4.add_argument("--llm-polish", action="store_true")
    p4.add_argument("--negation-mode", choices=["omit", "state"], default="omit")

    sub.add_parser("status", help="show pipeline progress")
    p_ui = sub.add_parser("ui", help="launch the local web UI")
    p_ui.add_argument("--port", type=int, default=8790)

    args = ap.parse_args()

    if args.cmd == "ui":
        import server
        server.main()
        return

    if args.cmd == "status":
        out = Path("out")
        cfg = Path("config")
        checks = [
            ("stage1 inventory", (out / "inventory.json").exists()),
            ("stage2 glossary", (cfg / "glossary.json").exists()),
            ("stage3 split", (out / "split_manifest.json").exists()),
            ("stage4 notes", (out / "notes").exists()),
        ]
        for name, done in checks:
            print(f"  [{'x' if done else ' '}] {name}")
        return

    if args.cmd == "run":
        common = ["--input", args.input, "--out", args.out]
        for stage_args in (
            ["stage1_inventory.py", common],
            ["stage2_glossary.py", ["--inventory", f"{args.out}/inventory.json",
                                    "--out", args.out, "--config", args.config]],
            ["stage3_split.py", common],
            ["stage4_convert.py", ["--patients-dir", f"{args.out}/patients",
                                   "--glossary", "config/glossary_stage4.json",
                                   "--out", args.out,
                                   "--negation-mode", args.negation_mode]
             + (["--llm-polish"] if args.llm_polish else [])],
        ):
            if run_stage(stage_args[0], stage_args[1]):
                sys.exit(1)
        sys.exit(0)

    if args.cmd == "stage4":
        s_args = ["--patients-dir", args.patients_dir,
                  "--glossary", args.glossary, "--out", args.out]
        if args.llm_polish:
            s_args.append("--llm-polish")
        s_args += ["--negation-mode", args.negation_mode]
        sys.exit(run_stage("stage4_convert.py", s_args))

    script = {"stage1": "stage1_inventory.py",
              "stage2": "stage2_glossary.py",
              "stage3": "stage3_split.py"}[args.cmd]
    s_args = []
    if args.input:
        s_args += ["--input", args.input]
    if args.cmd == "stage2":
        s_args += ["--inventory", args.inventory, "--config", args.config]
    s_args += ["--out", args.out]
    sys.exit(run_stage(script, s_args))


if __name__ == "__main__":
    main()
