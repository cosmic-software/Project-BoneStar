#!/usr/bin/env python3
"""Local server for the WebVerse viewer.

    python tools/serve.py            (port 8000)

Serves the project root (like `python -m http.server 8000`, with content types for .veml,
.glb and .json) plus one action:

    GET /api/update   refresh the TLEs from CelesTrak (tools/update_tles.py), then rerun the
                      fleet job (tools/run_fleet.py: GMAT headless -> OEMs -> data/fleet.json
                      and orbit lines). Returns JSON {"ok": bool, "output": "..."}.

    GET /api/save?file=data/charts/<name>.png
                      copy that plot (and the SVG beside it) to exports/<name>_<UTC time>.png
                      / .svg. Returns JSON {"ok": bool, "saved": [paths]}. Only files in
                      data/charts/ are accepted.

    GET /api/plot?key=<site or craft key>&span=week|month|6mo|year
                      that plot over the span (tools/plots.py; cached for the fleet run). The
                      first request starts it in the background (a span needing a GMAT
                      propagation takes ~25 s, six months / a year ~2.5 min) and answers
                      {"ok": false, "pending": true}; ask again until {"ok": true, "file": ...}
                      (or {"ok": false, "error": ...}).

The viewer's "Update TLEs" button calls the first and then reloads data/fleet.json; the save
icon on its plot panels calls the second. Only one update runs at a time. Requests are logged to stdout and data/server.log, including the viewer's
/__diag reports.
"""
import json
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
PORT = 8000
CHARTS = ROOT / "data" / "charts"
sys.path.insert(0, str(ROOT / "tools"))
plot_lock = threading.Lock()         # matplotlib (and GMAT) one plot at a time
plot_jobs = {}                       # (key, span) -> "running" or ("error", message)


def plot_job(key, span):
    import plots
    try:
        with plot_lock:
            rel = plots.span_plot(key, span)
        plot_jobs.pop((key, span), None)
        print(f"plot {key} {span}: {rel}", flush=True)
    except Exception as e:           # reported to the viewer on its next request
        plot_jobs[(key, span)] = ("error", str(e))
        print(f"plot {key} {span}: failed ({e})", flush=True)
EXPORTS = ROOT / "exports"
update_lock = threading.Lock()


def save_plot(rel):
    """Copy data/charts/<name>.png and its .svg into exports/ with the time saved."""
    src = (ROOT / rel).resolve()
    if src.parent != CHARTS.resolve() or src.suffix != ".png" or not src.exists():
        return False, []
    EXPORTS.mkdir(exist_ok=True)
    base = src.stem.rsplit("_", 1)[0]                 # drop the run's stamp
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    saved = []
    for ext in (".png", ".svg"):
        f = src.with_suffix(ext)
        if f.exists():
            dst = EXPORTS / f"{base}_{stamp}{ext}"
            shutil.copyfile(f, dst)
            saved.append(dst.relative_to(ROOT).as_posix())
    return True, saved


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
        if self.path.split("?")[0] == "/api/plot":
            q = parse_qs(urlparse(self.path).query)
            key, span = q.get("key", [""])[0], q.get("span", [""])[0]
            import plots
            try:
                rel = plots.cached(key, span)
            except (OSError, ValueError) as e:
                return self.send_json(400, {"ok": False, "error": str(e)})
            if rel:
                return self.send_json(200, {"ok": True, "file": rel})
            job = plot_jobs.get((key, span))
            if isinstance(job, tuple):
                plot_jobs.pop((key, span), None)
                return self.send_json(400, {"ok": False, "error": job[1]})
            if job is None:
                plot_jobs[(key, span)] = "running"
                threading.Thread(target=plot_job, args=(key, span), daemon=True).start()
            return self.send_json(200, {"ok": False, "pending": True})
        if self.path.split("?")[0] == "/api/save":
            rel = parse_qs(urlparse(self.path).query).get("file", [""])[0]
            ok, saved = save_plot(rel)
            print(f"save {rel}: {'saved ' + ', '.join(saved) if ok else 'refused'}", flush=True)
            return self.send_json(200 if ok else 400, {"ok": ok, "saved": saved})
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


class Tee:
    """stdout to the terminal and to data/server.log (the viewer's /__diag reports included),
    so the log can be read after the fact."""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for st in self.streams:
            st.write(text)

    def flush(self):
        for st in self.streams:
            st.flush()


if __name__ == "__main__":
    sys.stdout = Tee(sys.stdout, open(ROOT / "data" / "server.log", "a", encoding="utf-8", buffering=1))
    server = ThreadingHTTPServer(("", PORT), partial(Handler, directory=str(ROOT)))
    print(f"Serving {ROOT} on http://localhost:{PORT}/  (viewer: /webverse/index.veml)", flush=True)
    server.serve_forever()
