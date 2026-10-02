#!/usr/bin/env python3
"""Local server for the WebVerse viewer.

    python tools/serve.py            (port 8000)

Serves the project root (like `python -m http.server 8000`, with content types for .veml,
.glb and .json) plus one action:

    GET /api/update   refresh the TLEs from CelesTrak (tools/update_tles.py), then rerun the
                      fleet job (tools/run_fleet.py: GMAT headless -> OEMs -> data/fleet.json
                      and orbit lines). Returns JSON {"ok": bool, "output": "..."}.

The viewer's "Update TLEs" button calls it and then reloads data/fleet.json. Only one update
runs at a time. Requests are logged to stdout, including the viewer's /__diag reports.
"""
import json
import subprocess
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8000
update_lock = threading.Lock()


def run_update():
    output, ok = [], True
    for script in ("update_tles.py", "run_fleet.py"):
        run = subprocess.run([sys.executable, str(ROOT / "tools" / script)], cwd=ROOT,
                             capture_output=True, text=True)
        output.append(f"$ {script}\n{run.stdout}{run.stderr}")
        if run.returncode != 0:
            # a failed TLE download keeps the old TLE, so the fleet job can still run
            if script == "run_fleet.py":
                ok = False
    return ok, "\n".join(output)


class Handler(SimpleHTTPRequestHandler):
    # Content types http.server doesn't know (from the original dev server in this file):
    # without them .veml/.glb go out as application/octet-stream.
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map,
                      ".veml": "application/xml", ".glb": "model/gltf-binary", ".json": "application/json"}

    def do_GET(self):
        if self.path.split("?")[0] == "/api/update":
            if not update_lock.acquire(blocking=False):
                return self.send_json(409, {"ok": False, "output": "an update is already running"})
            try:
                ok, output = run_update()
            finally:
                update_lock.release()
            print(output, flush=True)
            return self.send_json(200 if ok else 500, {"ok": ok, "output": output})
        return super().do_GET()

    def send_json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stdout.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))
        sys.stdout.flush()


if __name__ == "__main__":
    server = ThreadingHTTPServer(("", PORT), partial(Handler, directory=str(ROOT)))
    print(f"Serving {ROOT} on http://localhost:{PORT}/  (viewer: /webverse/index.veml)", flush=True)
    server.serve_forever()
