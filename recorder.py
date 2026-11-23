#!/usr/bin/env python3
"""
owncast-vod: records a 24/7 Owncast stream into fixed-length VOD files
(default 4 hours, aligned to the clock: 00:00, 04:00, 08:00 ...) and serves
a small VOD browser page you can link from Owncast as an External Action.

Only needs Python 3.9+ (stdlib) and ffmpeg.
"""
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------- config ----
OWNCAST_URL = os.environ.get("OWNCAST_URL", "http://owncast:8080").rstrip("/")
# Override to pin one quality, e.g. "/hls/0/stream.m3u8". Default = master
# playlist; ffmpeg then picks the highest-resolution variant.
HLS_PATH = os.environ.get("HLS_PATH", "/hls/stream.m3u8")
VOD_DIR = Path(os.environ.get("VOD_DIR", "/vods"))
SEGMENT_SECONDS = int(os.environ.get("SEGMENT_SECONDS", str(4 * 3600)))
RETENTION_DAYS = float(os.environ.get("RETENTION_DAYS", "14"))  # 0 = keep forever
MIN_FREE_GB = float(os.environ.get("MIN_FREE_GB", "5"))  # delete oldest below this
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "15"))
PORT = int(os.environ.get("PORT", "8090"))
BIND = os.environ.get("BIND", "0.0.0.0")
TITLE = os.environ.get("TITLE", "Stream VODs")
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "")  # optional ?token=...

FILE_RE = re.compile(r"^vod_(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})(\d{2})\.mp4$")


def log(*a):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), *a, flush=True)


# -------------------------------------------------------------- recorder ----
class Recorder:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self.wake = threading.Event()

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def stream_online(self):
        try:
            with urllib.request.urlopen(OWNCAST_URL + "/api/status", timeout=10) as r:
                return bool(json.load(r).get("online"))
        except Exception as e:
            log("status check failed:", e)
            return False

    def start(self):
        with self.lock:
            if self.running:
                return
            VOD_DIR.mkdir(parents=True, exist_ok=True)
            src = OWNCAST_URL + HLS_PATH
            cmd = [
                FFMPEG, "-hide_banner", "-loglevel", "warning", "-nostdin",
                # treat a stalled live playlist (stream went offline) as EOF
                "-rw_timeout", "20000000",
                "-live_start_index", "-1",
                "-i", src,
                "-c", "copy",
                "-bsf:a", "aac_adtstoasc",
                "-f", "segment",
                "-segment_time", str(SEGMENT_SECONDS),
                "-segment_atclocktime", "1",
                "-reset_timestamps", "1",
                "-strftime", "1",
                "-segment_format", "mp4",
                # fragmented MP4: playable while still recording & crash-safe
                "-segment_format_options",
                "movflags=+frag_keyframe+empty_moov+default_base_moof",
                str(VOD_DIR / "vod_%Y-%m-%d_%H%M%S.mp4"),
            ]
            log("recording", src, "in", SEGMENT_SECONDS, "s chunks")
            self.proc = subprocess.Popen(cmd)

    def stop(self):
        with self.lock:
            if self.running:
                log("stopping recorder")
                self.proc.send_signal(signal.SIGINT)  # lets ffmpeg finalize
                try:
                    self.proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None

    def loop(self):
        while True:
            online = self.stream_online()
            if online and not self.running:
                self.start()
            elif not online and self.running:
                self.stop()
            cleanup()
            self.wake.wait(POLL_SECONDS)
            self.wake.clear()


rec = Recorder()


# --------------------------------------------------------------- storage ----
def list_vods():
    out = []
    if not VOD_DIR.exists():
        return out
    for p in VOD_DIR.iterdir():
        m = FILE_RE.match(p.name)
        if m and p.is_file():
            st = p.stat()
            out.append({
                "name": p.name,
                "date": m.group(1),
                "start": f"{m.group(2)}:{m.group(3)}",
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
    out.sort(key=lambda v: v["name"], reverse=True)
    if out and rec.running:
        out[0]["recording"] = True
    return out


def cleanup():
    vods = sorted(list_vods(), key=lambda v: v["name"])  # oldest first
    finished = [v for v in vods if not v.get("recording")]
    if RETENTION_DAYS > 0:
        cutoff = time.time() - RETENTION_DAYS * 86400
        for v in list(finished):
            if v["mtime"] < cutoff:
                _delete(v, "older than retention")
                finished.remove(v)
    if MIN_FREE_GB > 0 and VOD_DIR.exists():
        while finished and shutil.disk_usage(VOD_DIR).free < MIN_FREE_GB * 1e9:
            _delete(finished.pop(0), "low disk space")


def _delete(v, why):
    try:
        (VOD_DIR / v["name"]).unlink()
        log("deleted", v["name"], f"({why})")
    except OSError as e:
        log("delete failed", v["name"], e)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ------------------------------------------------------------------ http ----
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
body{{font-family:system-ui,sans-serif;background:#111;color:#eee;margin:0;padding:16px;max-width:900px;margin:auto}}
h1{{font-size:1.3rem}} video{{width:100%;background:#000;border-radius:8px;display:none;margin-bottom:12px}}
.day{{margin-top:18px;color:#aaa;font-size:.9rem;border-bottom:1px solid #333;padding-bottom:4px}}
.v{{display:flex;justify-content:space-between;align-items:center;padding:10px;border-radius:6px;cursor:pointer}}
.v:hover{{background:#222}} .live{{color:#f55;font-weight:600;margin-left:6px}}
a{{color:#8ab4f8}} small{{color:#888}}
</style></head><body>
<h1>{title}</h1><p><a href="/">← back to live</a></p><video id="p" controls playsinline></video>
{rows}
<script>
document.querySelectorAll('.v').forEach(r=>r.onclick=e=>{{
 if(e.target.tagName==='A')return;const p=document.getElementById('p');
 p.style.display='block';p.src='/vods/'+r.dataset.f;p.play();scrollTo(0,0);}});
</script></body></html>"""


def render_page():
    vods = list_vods()
    if not vods:
        rows = "<p>No VODs yet.</p>"
    else:
        rows, day = [], None
        for v in vods:
            if v["date"] != day:
                day = v["date"]
                rows.append(f'<div class="day">{day}</div>')
            f = html.escape(v["name"])
            live = '<span class="live">● recording</span>' if v.get("recording") else ""
            rows.append(
                f'<div class="v" data-f="{f}"><span>▶ {v["start"]}{live}</span>'
                f'<span><small>{human(v["size"])}</small> &nbsp;'
                f'<a href="/vods/{f}" download>download</a></span></div>')
        rows = "\n".join(rows)
    return PAGE.format(title=html.escape(TITLE), rows=rows).encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="text/plain", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/vods", "/vods/"):
            return self._send(200, render_page(), "text/html; charset=utf-8")
        if path in ("/", ""):
            return self._send(302, extra={"Location": "/vods"})
        if path == "/vods/api/list":
            return self._send(200, json.dumps(list_vods()).encode(), "application/json")
        if path.startswith("/vods/"):
            return self.serve_file(urllib.parse.unquote(path[6:]))
        self._send(404, b"not found")

    def do_POST(self):
        """Owncast webhook endpoint: STREAM_STARTED / STREAM_STOPPED."""
        u = urllib.parse.urlparse(self.path)
        if u.path not in ("/webhook", "/vods/webhook"):
            return self._send(404, b"not found")
        if WEBHOOK_TOKEN and urllib.parse.parse_qs(u.query).get("token", [""])[0] != WEBHOOK_TOKEN:
            return self._send(403, b"bad token")
        n = int(self.headers.get("Content-Length") or 0)
        try:
            event = json.loads(self.rfile.read(n) or b"{}").get("type", "")
        except ValueError:
            event = ""
        log("webhook", event or "(unknown)")
        if event in ("STREAM_STARTED", "STREAM_STOPPED"):
            rec.wake.set()  # re-check status now instead of waiting for poll
        self._send(200, b"ok")

    def serve_file(self, name):
        if not FILE_RE.match(name):
            return self._send(404, b"not found")
        p = VOD_DIR / name
        if not p.is_file():
            return self._send(404, b"not found")
        size = p.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
        if m:
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            elif m.group(2):  # suffix range: last N bytes
                start = max(0, size - int(m.group(2)))
            if start > end:
                return self._send(416, extra={"Content-Range": f"bytes */{size}"})
        length = end - start + 1
        self.send_response(206 if m else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Access-Control-Allow-Origin", "*")
        if m:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(p, "rb") as f:
            f.seek(start)
            left = length
            try:
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass


def main():
    log(f"owncast-vod: {OWNCAST_URL}, chunks={SEGMENT_SECONDS}s, dir={VOD_DIR}, port={PORT}")
    threading.Thread(target=rec.loop, daemon=True).start()

    def shutdown(*_):
        rec.stop()
        sys.exit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
