"""Local web UI for the FHIR-to-notes pipeline.

Run:  python3 src/f2n.py ui     (then open http://localhost:8790)

Endpoints:
  GET  /                 single-page UI
  GET  /api/state        pipeline status, patients, gate summary, log tail
  GET  /api/note/<pid>   one patient's note + gate detail
  GET  /api/glossary     glossary terms grouped by status
  POST /api/approve      {term_id} -> force-approve in the Stage-4 working copy
  POST /api/run          {"input": "<dir>"} -> executes stages 1-4 locally
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
CONFIG = ROOT / "config"
STATE = {"running": False, "log": []}

def _ui_html() -> str:
    """Read fresh on every request so UI edits show up without a restart."""
    return (Path(__file__).parent / "ui.html").read_text(encoding="utf-8")


def _run_pipeline(cmd: list[str]):
    STATE.update(running=True, log=[])
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
        for line in proc.stdout:
            STATE["log"].append(line.rstrip())
            STATE["log"] = STATE["log"][-400:]
        proc.wait()
        STATE["log"].append(f"[exit code {proc.returncode}]")
    finally:
        STATE["running"] = False


def _load_gate() -> dict:
    p = OUT / "gate_report.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _state_payload() -> dict:
    gate = _load_gate()
    patients = []
    pdir = OUT / "patients"
    if pdir.exists():
        for bf in sorted(pdir.glob("*.fhir.json")):
            pid = bf.name.replace(".fhir.json", "")
            v = gate.get("patients", {}).get(pid, {})
            note_file = Path(v["note_file"]) if v.get("note_file") else None
            note = note_file.read_text().strip() if note_file and note_file.exists() else None
            bundle = json.loads(bf.read_text())
            types = {}
            for e in bundle.get("entry", []):
                t = e["resource"].get("resourceType")
                types[t] = types.get(t, 0) + 1
            negated = []
            fpath = OUT / "facts" / f"{pid}.facts.json"
            if fpath.exists():
                fd = json.loads(fpath.read_text())["facts"]
                for group in ("conditions", "observations", "procedures",
                              "medications"):
                    for f in fd.get(group, []):
                        if f.get("negated"):
                            negated.append({
                                "kind": group[:-1], "term": f.get("term"),
                                "value": f.get("value"), "unit": f.get("unit"),
                            })
            prov = {}
            pprov = Path(v["provenance_file"]) if v.get("provenance_file") else None
            if pprov and pprov.exists():
                prov = json.loads(pprov.read_text())
            patients.append({
                "id": pid,
                "verdict": v.get("verdict") or ("NOTE MISSING" if not note else "?"),
                "provenance": {
                    "bundle_sha256": prov.get("source_bundle", {}).get("sha256"),
                    "resource_ids": prov.get("source_bundle", {}).get("resource_ids", []),
                    "glossary_sha256": prov.get("glossary", {}).get("sha256"),
                    "negation_mode": prov.get("negation_mode"),
                    "llm_polish": prov.get("llm_polish"),
                    "input_files": list(prov.get("split_manifest", {})
                                        .get("input_files_with_hashes", {}).keys()),
                },
                "resources": len(bundle.get("entry", [])),
                "types": types,
                "invented_numbers": v.get("invented_numbers"),
                "negation_flips": v.get("negation_flips"),
                "dropped_facts": [d["term"] for d in v.get("dropped_facts", [])],
                "negated_facts": negated,
                "note": note,
            })
    return {
        "running": STATE["running"],
        "log": STATE["log"][-40:],
        "patients": patients,
        "has_inventory": (OUT / "inventory.json").exists(),
        "has_glossary": (CONFIG / "glossary.json").exists(),
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body.encode())))
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, _ui_html(), "text/html")
        elif self.path == "/api/state":
            self._send(200, json.dumps(_state_payload()))
        elif self.path.startswith("/api/download/"):
            pid = self.path.rsplit("/", 1)[-1]
            safe = Path(pid).name
            for folder in ("notes", "rejected"):
                f = OUT / folder / f"{safe}.txt"
                if f.exists():
                    body = f.read_text()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Disposition",
                                     f'attachment; filename="{safe}_note.txt"')
                    self.send_header("Content-Length", str(len(body.encode())))
                    self.end_headers()
                    self.wfile.write(body.encode())
                    return
            self._send(404, '{"error":"no note"}')
        elif self.path.startswith("/api/provenance/"):
            pid = Path(self.path.rsplit("/", 1)[-1]).name
            gate = _load_gate()
            pf = (gate.get("patients", {}).get(pid, {}) or {}).get("provenance_file")
            if pf and Path(pf).exists():
                self._send(200, Path(pf).read_text())
            else:
                self._send(404, '{"error":"no provenance"}')
        elif self.path.startswith("/api/glossary"):
            # Union view: authoritative glossary (may hold NEW pending terms
            # from fresh data) merged with the Stage-4 working copy (holds
            # statuses Stage 4 actually consumes). Approving writes into the
            # working copy so Stage 4 can see the term.
            auth = {}
            ap = CONFIG / "glossary.json"
            if ap.exists():
                auth = json.loads(ap.read_text()).get("terms", {})
            wp = CONFIG / "glossary_stage4.json"
            work = json.loads(wp.read_text()).get("terms", {}) if wp.exists() else {}
            merged = {}
            for cid, t in {**auth, **work}.items():
                # Stage 4 consumes ONLY the working copy. A term absent from it
                # is effectively unresolved for note generation - even if the
                # authoritative file auto-resolved it. So absence from the
                # working copy always presents as pending, with an approve
                # button that copies it across.
                if cid not in work:
                    status = "pending_review" if t.get("en") else "unresolved"
                else:
                    status = work[cid].get("status", "pending_review")
                merged[cid] = {**t, "status": status,
                               "is_new": cid not in work}
            self._send(200, json.dumps({"terms": merged}, ensure_ascii=False))
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or "{}")
        if self.path == "/api/run":
            if STATE["running"]:
                self._send(409, '{"error":"pipeline already running"}')
                return
            input_dir = body.get("input", "").strip()
            if not input_dir or not Path(input_dir).expanduser().exists():
                self._send(400, '{"error":"input directory does not exist"}')
                return
            negmode = body.get("negmode", "omit")
            cmd = [sys.executable, str(ROOT / "src" / "f2n.py"), "run",
                   "--input", str(Path(input_dir).expanduser()),
                   "--negation-mode", negmode]
            if body.get("polish"):
                cmd.append("--llm-polish")
            threading.Thread(target=_run_pipeline, args=(cmd,),
                             daemon=True).start()
            self._send(202, '{"started": true}')
        elif self.path == "/api/approve":
            gp = CONFIG / "glossary_stage4.json"
            data = json.loads(gp.read_text())
            term = body.get("term_id")
            auth = {}
            ap = CONFIG / "glossary.json"
            if ap.exists():
                auth = json.loads(ap.read_text()).get("terms", {})
            if term in data["terms"]:
                data["terms"][term]["status"] = "approved"
            elif term in auth and auth[term].get("en"):
                data["terms"][term] = {**auth[term], "status": "approved"}
            else:
                self._send(404, '{"error":"unknown term"}')
                return
            gp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            self._send(200, '{"approved": true}')
        else:
            self._send(404, '{"error":"not found"}')

    def log_message(self, *a):
        pass


def main():
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8790
    print(f"fhir-to-notes UI: http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
