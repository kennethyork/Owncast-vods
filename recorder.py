#!/usr/bin/env python3
"""
owncast-vod: records a 24/7 Owncast stream into fixed-length VOD files
(default 4 hours, aligned to the clock: 00:00, 04:00, 08:00 ...) and serves
a small VOD browser page you can link from Owncast as an External Action.

Only needs Python 3.9+ (stdlib) and ffmpeg.
"""
import base64
import hashlib
import hmac
import html
import json
import xml.etree.ElementTree as ET
import os
import re
import secrets
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
LIVE_URL = os.environ.get("LIVE_URL", "/")  # "back to live" link
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "")  # optional ?token=...
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")  # download gate; empty = open
# WebDAV base URL served by the rclone backup container (cloud playback).
# Empty string disables cloud listing entirely.
CLOUD_URL = os.environ.get("CLOUD_URL", "").rstrip("/")  # e.g. http://127.0.0.1:8091

FILE_RE = re.compile(r"^vod_(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})(\d{2})\.mp4$")

# ---------------------------------------------------- auth helpers ----------
def _hash(pw: str) -> str:
    """SHA-256 hex of the password (compare tokens server-side, never raw PW)."""
    return hashlib.sha256(pw.encode()).hexdigest()

# A random 32-byte secret minted per-process to sign session tokens.
_SESSION_SECRET = secrets.token_hex(32)

def _make_token() -> str:
    sig = hmac.new(_SESSION_SECRET.encode(), b"admin", "sha256").hexdigest()
    return f"admin:{sig}"

def _valid_token(tok: str) -> bool:
    if not tok:
        return False
    try:
        expected = _make_token()
        return hmac.compare_digest(tok, expected)
    except Exception:
        return False

def _download_allowed(handler) -> bool:
    """Return True if the request carries a valid session cookie or no password is set."""
    if not ADMIN_PASSWORD:
        return True
    cookies = handler.headers.get("Cookie", "")
    for part in cookies.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "vod_admin" and _valid_token(v.strip()):
            return True
    return False


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


# ---------------------------------------------------------- cloud listing ----
import xml.etree.ElementTree as ET

_cloud_cache: list = []
_cloud_cache_time: float = 0.0
_CLOUD_TTL = 60  # seconds between WebDAV PROPFIND calls

_DAV_NS = "DAV:"


def _propfind(url: str) -> list:
    """Return list of dicts {name, size} from a WebDAV PROPFIND response."""
    req = urllib.request.Request(
        url + "/",
        method="PROPFIND",
        headers={"Depth": "1", "Content-Type": "application/xml"},
        data=b'<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:prop>'
             b'<D:displayname/><D:getcontentlength/></D:prop></D:propfind>',
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        body = r.read()
    root = ET.fromstring(body)
    out = []
    for resp in root.iter(f"{{{_DAV_NS}}}response"):
        href = (resp.findtext(f"{{{_DAV_NS}}}href") or "").rstrip("/")
        name = href.rsplit("/", 1)[-1]
        if not FILE_RE.match(name):
            continue
        cl = resp.findtext(
            f"{{{_DAV_NS}}}propstat/{{{_DAV_NS}}}prop/{{{_DAV_NS}}}getcontentlength"
        ) or "0"
        out.append({"name": name, "size": int(cl)})
    return out


def list_cloud_vods() -> list:
    """Return cloud-only VODs (not present locally), cached for _CLOUD_TTL seconds."""
    global _cloud_cache, _cloud_cache_time
    if not CLOUD_URL:
        return []
    now = time.time()
    if now - _cloud_cache_time < _CLOUD_TTL:
        return _cloud_cache
    try:
        remote = _propfind(CLOUD_URL)
        local_names = {v["name"] for v in list_vods()}
        result = []
        for f in remote:
            if f["name"] not in local_names:
                m = FILE_RE.match(f["name"])
                result.append({
                    "name": f["name"],
                    "date": m.group(1),
                    "start": f"{m.group(2)}:{m.group(3)}",
                    "size": f["size"],
                    "mtime": 0.0,
                    "cloud": True,
                })
        result.sort(key=lambda v: v["name"], reverse=True)
        _cloud_cache = result
        _cloud_cache_time = now
        return result
    except Exception as e:
        log("cloud list failed:", e)
        return _cloud_cache  # stale is better than nothing


def merged_vods() -> list:
    """Local VODs first, then cloud-only ones, sorted newest-first."""
    local = list_vods()
    cloud = list_cloud_vods()
    combined = local + cloud
    combined.sort(key=lambda v: v["name"], reverse=True)
    return combined


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
/* login modal */
#modal{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);align-items:center;justify-content:center;z-index:99}}
#modal.open{{display:flex}}
#mbox{{background:#1e1e1e;padding:24px;border-radius:10px;min-width:260px;text-align:center}}
#mbox h2{{margin:0 0 16px;font-size:1rem}} #mbox input{{width:100%;padding:8px;border:1px solid #444;border-radius:6px;background:#111;color:#eee;font-size:1rem;box-sizing:border-box}}
#mbox button{{margin-top:12px;width:100%;padding:9px;background:#8ab4f8;color:#000;border:0;border-radius:6px;font-size:1rem;cursor:pointer;font-weight:600}}
#merr{{color:#f55;font-size:.85rem;margin-top:8px;min-height:1em}}
</style></head><body>
<h1>{title}</h1><p><a href="{live}">← back to live</a></p><video id="p" controls playsinline></video>
{rows}
{login_modal}
<script>
document.querySelectorAll('.v').forEach(r=>r.onclick=e=>{{
 if(e.target.tagName==='A'||e.target.closest('button'))return;const p=document.getElementById('p');
 p.style.display='block';p.src=r.dataset.f;p.play();scrollTo(0,0);}});
{login_js}
</script></body></html>"""

LOGIN_MODAL = """<div id="modal"><div id="mbox">
<h2>Admin login to download</h2>
<input id="mpw" type="password" placeholder="Password" autocomplete="current-password">
<button onclick="doLogin()">Login</button>
<div id="merr"></div>
</div></div>"""

LOGIN_JS = """
var _dlFile=null,_dlCloud=false;
function openLogin(f,cloud){_dlFile=f;_dlCloud=cloud||false;
  document.getElementById('modal').classList.add('open');
  setTimeout(()=>document.getElementById('mpw').focus(),50);}
document.getElementById('modal').onclick=function(e){if(e.target===this)this.classList.remove('open');};
document.getElementById('mpw').addEventListener('keydown',e=>{if(e.key==='Enter')doLogin();});
function doLogin(){
  var pw=document.getElementById('mpw').value;
  fetch('/vods/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})})
  .then(r=>r.json()).then(d=>{
    if(d.ok){document.getElementById('modal').classList.remove('open');
              document.getElementById('mpw').value='';
              if(_dlFile){var a=document.createElement('a');
                a.href=_dlCloud?'/vods/cloud/'+_dlFile+'?dl=1':'/vods/download/'+_dlFile;
                a.download=_dlFile;a.click();}
    }else{document.getElementById('merr').textContent='Wrong password.';}
  });
}"""

OPEN_DOWNLOAD_JS = ""  # no modal needed when downloads are open


def render_page(authed: bool = False):
    """authed=True means the request already has a valid admin cookie."""
    vods = merged_vods()
    if not vods:
        rows = "<p>No VODs yet.</p>"
    else:
        rows, day = [], None
        gate = ADMIN_PASSWORD != ""
        for v in vods:
            if v["date"] != day:
                day = v["date"]
                rows.append(f'<div class="day">{day}</div>')
            f = html.escape(v["name"])
            is_cloud = v.get("cloud", False)
            live = '<span class="live">● recording</span>' if v.get("recording") else ""
            cloud_badge = ' <span title="stored in cloud" style="color:#8ab4f8;font-size:.8rem">☁</span>' if is_cloud else ""
            # src for the player: cloud files go through the proxy route
            player_src = f"/vods/cloud/{f}" if is_cloud else f"/vods/{f}"
            if gate and not authed:
                dl = f'<button style="background:none;border:none;cursor:pointer;color:#8ab4f8;font-size:.9rem" onclick="openLogin(\'{f}\',{str(is_cloud).lower()})">🔒 download</button>'
            else:
                dl_href = f"/vods/cloud/{f}?dl=1" if is_cloud else f"/vods/download/{f}"
                dl = f'<a href="{dl_href}" download>download</a>'
            rows.append(
                f'<div class="v" data-f="{player_src}"><span>▶ {v["start"]}{live}{cloud_badge}</span>'
                f'<span><small>{human(v["size"])}</small> &nbsp;{dl}</span></div>')
        rows = "\n".join(rows)

    if ADMIN_PASSWORD and not authed:
        modal = LOGIN_MODAL
        js = LOGIN_JS
    else:
        modal = ""
        js = OPEN_DOWNLOAD_JS

    return PAGE.format(
        title=html.escape(TITLE),
        live=html.escape(LIVE_URL, quote=True),
        rows=rows,
        login_modal=modal,
        login_js=js,
    ).encode()


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
            authed = _download_allowed(self)
            return self._send(200, render_page(authed), "text/html; charset=utf-8")
        if path in ("/", ""):
            return self._send(302, extra={"Location": "/vods"})
        if path == "/vods/api/list":
            return self._send(200, json.dumps(merged_vods()).encode(), "application/json")
        # /vods/download/<name>  — gated download (triggers Content-Disposition)
        if path.startswith("/vods/download/"):
            if not _download_allowed(self):
                return self._send(403, b"login required")
            name = urllib.parse.unquote(path[len("/vods/download/"):])
            return self.serve_file(name, download=True)
        # /vods/cloud/<name>  — proxy to rclone WebDAV (cloud-only files)
        if path.startswith("/vods/cloud/"):
            name = urllib.parse.unquote(path[len("/vods/cloud/"):])
            parsed = urllib.parse.urlparse(self.path)
            force_dl = "dl" in urllib.parse.parse_qs(parsed.query)
            if force_dl and not _download_allowed(self):
                return self._send(403, b"login required")
            return self.proxy_cloud(name, download=force_dl)
        # /vods/<name>  — streaming (always allowed; browser can play without auth)
        if path.startswith("/vods/"):
            return self.serve_file(urllib.parse.unquote(path[6:]))
        self._send(404, b"not found")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        # ---- Admin login ----
        if u.path == "/vods/login":
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                pw = body.get("password", "")
            except (ValueError, AttributeError):
                pw = ""
            if ADMIN_PASSWORD and hmac.compare_digest(_hash(pw), _hash(ADMIN_PASSWORD)):
                token = _make_token()
                resp = json.dumps({"ok": True}).encode()
                # HttpOnly so JS can't steal it; SameSite=Strict; no Secure flag
                # (the reverse proxy handles HTTPS termination)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.send_header("Set-Cookie",
                    f"vod_admin={token}; Path=/vods; HttpOnly; SameSite=Strict; Max-Age=86400")
                self.end_headers()
                self.wfile.write(resp)
            else:
                self._send(401, json.dumps({"ok": False}).encode(), "application/json")
            return
        # ---- Owncast webhook: STREAM_STARTED / STREAM_STOPPED ----
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

    def serve_file(self, name, download=False):
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
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
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


    def proxy_cloud(self, name, download=False):
        """Proxy a cloud VOD from rclone WebDAV, with Range passthrough.
        download=True adds Content-Disposition: attachment (saves the file).
        Without it the browser can play the video inline in the page player.
        """
        if not FILE_RE.match(name):
            return self._send(404, b"not found")
        if not CLOUD_URL:
            return self._send(503, b"cloud playback not configured")
        url = f"{CLOUD_URL}/{urllib.parse.quote(name)}"
        req_headers = {}
        rng = self.headers.get("Range")
        if rng:
            req_headers["Range"] = rng
        try:
            req = urllib.request.Request(url, headers=req_headers)
            upstream = urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError as e:
            return self._send(e.code, str(e).encode())
        except Exception as e:
            log("cloud proxy error:", e)
            return self._send(502, b"cloud unavailable")
        status = upstream.status
        self.send_response(status)
        for hdr in ("Content-Type", "Content-Length", "Content-Range",
                    "Accept-Ranges"):
            val = upstream.headers.get(hdr)
            if val:
                self.send_header(hdr, val)
        if download:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{name}"')
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command == "HEAD":
            upstream.close()
            return
        try:
            while True:
                chunk = upstream.read(1 << 20)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            upstream.close()


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
