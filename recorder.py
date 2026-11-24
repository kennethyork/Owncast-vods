#!/usr/bin/env python3
"""
owncast-vod: records a 24/7 Owncast stream into fixed-length VOD files
(default 4 hours, aligned to the clock: 00:00, 04:00, 08:00 ...) and serves
a small VOD browser page you can link from Owncast as an External Action.

Only needs Python 3.9+ (stdlib) and ffmpeg.
"""
import base64
import email.mime.multipart
import email.mime.text
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import signal
import smtplib
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
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
LIVE_URL = os.environ.get("LIVE_URL", "/")  # stream page; default for NOTIFY_URL
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "")  # optional ?token=...
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")  # download gate; empty = open
# WebDAV base URL served by the rclone backup container (cloud playback).
# Empty string disables cloud listing entirely.
CLOUD_URL = os.environ.get("CLOUD_URL", "").rstrip("/")  # e.g. http://127.0.0.1:8091
# Comments under VODs, signed with the viewer's Owncast chat name.
COMMENTS = os.environ.get("COMMENTS", "1") not in ("0", "false", "no", "")

# ---- Thumbnails ----
# Set to "" to disable thumbnail generation.
THUMB_ENABLED = os.environ.get("THUMB_ENABLED", "1") not in ("0", "false", "no", "")
THUMB_OFFSET = int(os.environ.get("THUMB_OFFSET", "60"))  # seconds into VOD to grab

# ---- Email notifications ----
SMTP_HOST = os.environ.get("SMTP_HOST", "")          # e.g. smtp.gmail.com
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)   # defaults to SMTP_USER
NOTIFY_TO = os.environ.get("NOTIFY_TO", "")          # comma-separated recipients
NOTIFY_URL = os.environ.get("NOTIFY_URL", LIVE_URL)  # link in the email body

FILE_RE = re.compile(r"^vod_(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})(\d{2})\.mp4$")

# ---------------------------------------------------- auth helpers ----------
def _hash(pw: str) -> str:
    """SHA-256 hex of the password (compare tokens server-side, never raw PW)."""
    return hashlib.sha256(pw.encode()).hexdigest()

SESSION_SECONDS = 86400  # admin login lifetime
PLAY_LINK_SECONDS = 12 * 3600  # signed playback links (must outlast a 4h VOD + pauses)


def _load_secret() -> bytes:
    """Random secret kept in VOD_DIR so sessions survive container restarts.
    The admin password is mixed in, so changing it logs everyone out."""
    path = VOD_DIR / ".secret"
    try:
        raw = path.read_text().strip()
    except OSError:
        raw = ""
    if len(raw) < 64:
        raw = secrets.token_hex(32)
        try:
            VOD_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(raw)
            path.chmod(0o600)
        except OSError as e:
            log("could not save session secret (logins reset on restart):", e)
    return hmac.new(raw.encode(), ADMIN_PASSWORD.encode(), "sha256").digest()


def _sign(msg: str) -> str:
    return hmac.new(_SECRET, msg.encode(), "sha256").hexdigest()


def _make_token() -> str:
    exp = int(time.time()) + SESSION_SECONDS
    return f"{exp}.{_sign(f'admin:{exp}')}"


def _valid_token(tok: str) -> bool:
    exp, _, sig = tok.partition(".")
    if not (exp.isascii() and exp.isdigit()) or int(exp) < time.time():
        return False
    return hmac.compare_digest(sig.encode(), _sign(f"admin:{exp}").encode())


def _is_admin(handler) -> bool:
    cookies = handler.headers.get("Cookie", "")
    for part in cookies.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "vod_admin" and _valid_token(v.strip()):
            return True
    return False


def _download_allowed(handler) -> bool:
    """Return True if the request carries a valid session cookie or no password is set."""
    return not ADMIN_PASSWORD or _is_admin(handler)


def _play_query(name: str) -> str:
    """Query string that lets the page player stream `name` for a limited time.
    Empty when there is no password (playback URLs are then plain and permanent)."""
    if not ADMIN_PASSWORD:
        return ""
    exp = int(time.time()) + PLAY_LINK_SECONDS
    return f"?exp={exp}&sig={_sign(f'play:{name}:{exp}')}"


def _play_allowed(handler, name: str) -> bool:
    """Streaming needs a signed link from the page (or an admin cookie) when a
    password is set, so the raw /vods/<file>.mp4 URL isn't a permanent download."""
    if _download_allowed(handler):
        return True
    q = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
    exp, sig = q.get("exp", [""])[0], q.get("sig", [""])[0]
    if not (exp.isascii() and exp.isdigit()) or int(exp) < time.time():
        return False
    return hmac.compare_digest(sig.encode(), _sign(f"play:{name}:{exp}").encode())


# ---- Login rate limiting: LOGIN_MAX_FAILS wrong passwords per IP per window ----
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW = 15 * 60
_login_fails: dict = {}  # ip -> [timestamps of recent failures]
_login_lock = threading.Lock()


def _client_ip(handler) -> str:
    """Real client IP. Behind a local reverse proxy every request comes from
    127.0.0.1, so use the last X-Forwarded-For hop (the one the proxy added)."""
    ip = handler.client_address[0]
    fwd = handler.headers.get("X-Forwarded-For", "")
    if fwd and ip in ("127.0.0.1", "::1"):
        ip = fwd.split(",")[-1].strip() or ip
    return ip


def _login_blocked(ip: str) -> bool:
    now = time.time()
    with _login_lock:
        recent = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_WINDOW]
        if recent:
            _login_fails[ip] = recent
        else:
            _login_fails.pop(ip, None)
        return len(recent) >= LOGIN_MAX_FAILS


def _login_failed(ip: str):
    with _login_lock:
        _login_fails.setdefault(ip, []).append(time.time())


# ---------------------------------------------------- VOD comments ----------
# /vods is on the Owncast domain, so the browser sends Owncast's
# owncast_chat_token cookie (v0.3.0+; older versions: localStorage, sent by the
# page as X-Chat-Token). Owncast has no "who am I" API: the only check is
# opening the chat websocket and reading CONNECTED_USER_INFO. While live that
# counts as joining chat ("X joined" + webhook), so tokens are only checked,
# and comments only posted, while the stream is offline.
COMMENT_MAX_LEN = 500
COMMENT_MAX_PER_VOD = 1000
COMMENT_MIN_GAP = 10  # seconds between comments from one chat user
IDENTITY_TTL = 3600  # reuse a verified token this long (limits "user parted" webhooks)
VERIFY_MAX = 10  # websocket checks per IP per VERIFY_WINDOW
VERIFY_WINDOW = 600
TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-+/=.]{8,256}$")
_identities: dict = {}  # sha256(token) -> (expires, status, user)
_verify_hits: dict = {}  # ip -> [timestamps of websocket checks]
_ident_lock = threading.Lock()
_comment_lock = threading.Lock()
_last_comment: dict = {}  # chat user id -> time of last comment


class CommentError(Exception):
    def __init__(self, code: int, err: str):
        super().__init__(err)
        self.code, self.err = code, err


class ChatAuthError(Exception):
    pass


def _ws_read_frame(f):
    hdr = f.read(2)
    if len(hdr) < 2:
        raise ChatAuthError("chat closed the connection")
    n = hdr[1] & 0x7F
    if n == 126:
        n = int.from_bytes(f.read(2), "big")
    elif n == 127:
        n = int.from_bytes(f.read(8), "big")
    if n > 1 << 20:
        raise ChatAuthError("chat frame too large")
    mask = f.read(4) if hdr[1] & 0x80 else b""
    data = f.read(n)
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return hdr[0] & 0x80, hdr[0] & 0x0F, data


def owncast_whoami(token: str):
    """Ask Owncast chat who owns `token`: ("ok", user) / ("invalid", None) /
    ("banned", None). Raises ChatAuthError if chat can't be asked."""
    u = urllib.parse.urlsplit(OWNCAST_URL)
    tls = u.scheme == "https"
    path = u.path.rstrip("/") + "/ws?accessToken=" + urllib.parse.quote(token, safe="")
    try:
        sock = socket.create_connection((u.hostname, u.port or (443 if tls else 80)), timeout=8)
    except OSError as e:
        raise ChatAuthError(f"cannot reach Owncast: {e}")
    try:
        if tls:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.netloc}\r\n"
                      "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                      "User-Agent: owncast-vods\r\n\r\n").encode())
        f = sock.makefile("rb")
        status = f.readline().decode("latin-1").strip()
        while f.readline() not in (b"\r\n", b"\n", b""):
            pass
        if " 101 " not in status + " ":
            raise ChatAuthError(f"chat refused the connection ({status[:60]}) - is chat disabled?")
        buf = b""
        for _ in range(20):
            fin, op, data = _ws_read_frame(f)
            if op == 8:  # close
                break
            if op not in (0, 1):  # ping/pong/binary
                continue
            buf += data
            if not fin:
                continue
            # Owncast batches several JSON messages per frame, one per line.
            for line in buf.splitlines():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                t = msg.get("type") if isinstance(msg, dict) else None
                if t == "CONNECTED_USER_INFO" and isinstance(msg.get("user"), dict):
                    return "ok", msg["user"]
                if t == "ERROR_NEEDS_REGISTRATION":
                    return "invalid", None
                if t == "ERROR_USER_DISABLED":
                    return "banned", None
            buf = b""
        raise ChatAuthError("chat sent no user info")
    except OSError as e:
        raise ChatAuthError(f"chat connection failed: {e}")
    finally:
        try:
            m = os.urandom(4)
            sock.sendall(b"\x88\x82" + m + bytes(b ^ m[i] for i, b in enumerate(b"\x03\xe8")))
        except OSError:
            pass
        sock.close()


def _cached_identity(token: str):
    """(status, user) if this token was checked recently, else None. No side effects."""
    hit = _identities.get(hashlib.sha256(token.encode()).hexdigest())
    if hit and hit[0] > time.time():
        return hit[1], hit[2]
    return None


def identify(token: str, ip: str) -> dict:
    """Owncast chat user for `token`, or CommentError. Only opens the chat
    websocket while offline (see above)."""
    with _ident_lock:
        hit = _cached_identity(token)
    if hit:
        status, user = hit
    else:
        if rec.stream_online():
            raise CommentError(409, "live")
        now = time.time()
        with _ident_lock:
            recent = [t for t in _verify_hits.get(ip, []) if now - t < VERIFY_WINDOW]
            if len(recent) >= VERIFY_MAX:
                _verify_hits[ip] = recent
                raise CommentError(429, "rate_limited")
            _verify_hits[ip] = recent + [now]
        try:
            status, user = owncast_whoami(token)
        except ChatAuthError as e:
            log("chat token check failed:", e)
            raise CommentError(502, "chat_unreachable")
        with _ident_lock:
            for k in [k for k, v in _identities.items() if v[0] <= now]:
                del _identities[k]
            ttl = IDENTITY_TTL if status == "ok" else 60
            _identities[hashlib.sha256(token.encode()).hexdigest()] = (now + ttl, status, user)
    if status == "invalid":
        raise CommentError(401, "no_chat")
    if status == "banned":
        raise CommentError(403, "banned")
    return user


def _is_mod(user: dict) -> bool:
    return "MODERATOR" in (user.get("scopes") or [])


def _public_user(user: dict) -> dict:
    return {"id": user.get("id"), "name": user.get("displayName"), "mod": _is_mod(user)}


def comments_path(vod_name: str) -> Path:
    return VOD_DIR / (vod_name[:-4] + ".comments.json")  # vod_*.mp4 → vod_*.comments.json


def load_comments(vod_name: str) -> list:
    try:
        items = json.loads(comments_path(vod_name).read_text())
        return items if isinstance(items, list) else []
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        log("could not read comments for", vod_name, e)
        return []


def save_comments(vod_name: str, items: list):
    path = comments_path(vod_name)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items))
    tmp.replace(path)


def log(*a):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), *a, flush=True)


_SECRET = _load_secret()


# -------------------------------------------------------------- recorder ----
class Recorder:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.online = False  # last polled stream status

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
        # VODs already announced by email, saved to disk so a restart doesn't
        # re-send one email per VOD. First run: everything on disk counts as seen.
        seen = load_seen()
        if seen is None:
            seen = {v["name"] for v in list_vods()}
            save_seen(seen)
        thumb_tried: set = set()
        while True:
            online = self.online = self.stream_online()
            if online and not self.running:
                self.start()
            elif not online and self.running:
                self.stop()
            cleanup()
            # Detect newly finished VODs (not currently recording).
            vods = list_vods()
            changed = False
            for v in vods:
                name = v["name"]
                if v.get("recording"):
                    continue
                if not v["thumb"] and name not in thumb_tried:
                    thumb_tried.add(name)
                    make_thumb_bg(name)
                if name not in seen:
                    seen.add(name)
                    changed = True
                    notify_bg(v)
            # Forget deleted VODs so the file doesn't grow forever.
            on_disk = {v["name"] for v in vods}
            if changed or not seen <= on_disk:
                seen &= on_disk
                save_seen(seen)
            self.wake.wait(POLL_SECONDS)
            self.wake.clear()


rec = Recorder()

SEEN_FILE = VOD_DIR / ".notified.json"


def load_seen():
    """Set of VOD names already notified, or None if never saved."""
    try:
        return set(json.loads(SEEN_FILE.read_text()))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log("could not read", SEEN_FILE.name, "- treating all VODs as seen:", e)
        return None


def save_seen(seen: set):
    tmp = SEEN_FILE.with_suffix(".tmp")
    try:
        VOD_DIR.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(sorted(seen)))
        tmp.replace(SEEN_FILE)
    except OSError as e:
        log("could not save", SEEN_FILE.name, e)


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
                "thumb": thumb_path(p.name).exists(),
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
    # Remove the matching thumbnail too. Comments stay while the VOD can still
    # be watched from the cloud copy.
    extras = [thumb_path(v["name"])] + ([] if CLOUD_URL else [comments_path(v["name"])])
    for p in extras:
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ---------------------------------------------------------- thumbnails ------
def thumb_path(vod_name: str) -> Path:
    return VOD_DIR / (vod_name[:-4] + ".jpg")  # vod_*.mp4 → vod_*.jpg


def make_thumb(vod_name: str):
    """Extract a JPEG thumbnail at THUMB_OFFSET seconds using ffmpeg.
    Runs in a background thread; silently skips if ffmpeg fails."""
    if not THUMB_ENABLED:
        return
    mp4 = VOD_DIR / vod_name
    jpg = thumb_path(vod_name)
    if jpg.exists():
        return  # already done
    try:
        result = subprocess.run(
            [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-ss", str(THUMB_OFFSET),
                "-i", str(mp4),
                "-vframes", "1",
                "-vf", "scale=640:-1",
                "-q:v", "5",
                "-y", str(jpg),
            ],
            timeout=30,
            capture_output=True,
        )
        if result.returncode == 0 and jpg.exists():
            log("thumbnail created:", jpg.name)
        else:
            log("thumbnail failed for", vod_name, result.stderr.decode(errors="replace")[:200])
    except Exception as e:
        log("thumbnail error:", e)


def make_thumb_bg(vod_name: str):
    """Kick off thumbnail generation in a daemon thread."""
    threading.Thread(target=make_thumb, args=(vod_name,), daemon=True).start()


# ------------------------------------------------------- email notify -------
def send_email(vod: dict):
    """Send a notification email when a VOD finishes. No-op if SMTP not configured."""
    if not (SMTP_HOST and NOTIFY_TO and SMTP_USER):
        return
    recipients = [r.strip() for r in NOTIFY_TO.split(",") if r.strip()]
    if not recipients:
        return
    name = vod["name"]
    date = vod["date"]
    start = vod["start"]
    size = human(vod["size"])
    vod_url = NOTIFY_URL.rstrip("/") + f"/vods"

    subject = f"[{TITLE}] New VOD ready – {date} {start}"
    body_text = (
        f"A new VOD is ready on {TITLE}.\n\n"
        f"  Date:  {date}\n"
        f"  Start: {start}\n"
        f"  Size:  {size}\n"
        f"  File:  {name}\n\n"
        f"Watch or download at: {vod_url}\n"
    )
    body_html = f"""<html><body style="font-family:sans-serif;color:#222">
<h2 style="color:#333">New VOD ready on {html.escape(TITLE)}</h2>
<table style="border-collapse:collapse">
  <tr><td style="padding:4px 12px 4px 0;color:#666">Date</td><td><b>{html.escape(date)}</b></td></tr>
  <tr><td style="padding:4px 12px 4px 0;color:#666">Start</td><td><b>{html.escape(start)}</b></td></tr>
  <tr><td style="padding:4px 12px 4px 0;color:#666">Size</td><td>{html.escape(size)}</td></tr>
  <tr><td style="padding:4px 12px 4px 0;color:#666">File</td><td><code>{html.escape(name)}</code></td></tr>
</table>
<p style="margin-top:20px">
  <a href="{html.escape(vod_url, quote=True)}" style="background:#333;color:#fff;padding:10px 20px;border-radius:6px;text-decoration:none">
    Watch / Download →
  </a>
</p>
</body></html>"""

    msg = email.mime.multipart.MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM or SMTP_USER
    msg["To"] = ", ".join(recipients)
    msg.attach(email.mime.text.MIMEText(body_text, "plain"))
    msg.attach(email.mime.text.MIMEText(body_html, "html"))

    try:
        ctx = ssl.create_default_context()
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=15) as s:
                s.login(SMTP_USER, SMTP_PASS)
                s.sendmail(msg["From"], recipients, msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.login(SMTP_USER, SMTP_PASS)
                s.sendmail(msg["From"], recipients, msg.as_string())
        log("email sent to", ", ".join(recipients), "for", name)
    except Exception as e:
        log("email failed:", e)


def notify_bg(vod: dict):
    threading.Thread(target=send_email, args=(vod,), daemon=True).start()


# ---------------------------------------------------------- cloud listing ----
_cloud_cache: list = []
_cloud_cache_time: float = 0.0
_CLOUD_TTL = 60  # seconds between WebDAV PROPFIND calls

_DAV_NS = "DAV:"


THUMB_RE = re.compile(r"^vod_\d{4}-\d{2}-\d{2}_\d{6}\.jpg$")


def _propfind(url: str) -> list:
    """Return list of dicts {name, size} (VODs and thumbnails) from a WebDAV
    PROPFIND response."""
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
        name = urllib.parse.unquote(href.rsplit("/", 1)[-1])
        if not (FILE_RE.match(name) or THUMB_RE.match(name)):
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
        remote_names = {f["name"] for f in remote}
        result = []
        for f in remote:
            m = FILE_RE.match(f["name"])
            if m and f["name"] not in local_names:
                result.append({
                    "name": f["name"],
                    "date": m.group(1),
                    "start": f"{m.group(2)}:{m.group(3)}",
                    "size": f["size"],
                    "mtime": 0.0,
                    "cloud": True,
                    "thumb": f["name"][:-4] + ".jpg" in remote_names,
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


def vod_exists(name: str) -> bool:
    if not FILE_RE.match(name):
        return False
    return (VOD_DIR / name).is_file() or any(v["name"] == name for v in list_cloud_vods())


# ------------------------------------------------------------------ http ----
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
:root{{--bg:#0e0f13;--card:#171922;--card2:#1e212c;--line:#262a37;--text:#ececf1;--muted:#8d91a5;--gold:#f2b544;--red:#ff4d5e}}
*{{box-sizing:border-box}}
body{{font-family:Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--text);margin:0 auto;padding:24px 20px 40px;max-width:1200px;-webkit-font-smoothing:antialiased}}
a{{color:var(--gold);text-decoration:none}} a:hover{{text-decoration:underline}}
.top{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:20px;flex-wrap:wrap}}
.top h1{{margin:0;font-size:1.6rem;font-weight:800;letter-spacing:-.02em}}
.top .sub{{color:var(--muted);font-size:.9rem;margin-top:2px}}
/* player */
#stage{{display:none;background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden;margin-bottom:28px;box-shadow:0 10px 30px rgba(0,0,0,.35)}}
#stage.on{{display:block}}
video{{width:100%;display:block;background:#000;aspect-ratio:16/9}}
.np{{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 18px;border-bottom:1px solid var(--line)}}
.np-k{{color:var(--gold);font-size:.72rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase}}
#nptitle{{font-weight:700;font-size:1.05rem;margin-top:2px}}
#npclose{{background:none;border:1px solid var(--line);color:var(--muted);border-radius:8px;padding:6px 10px;cursor:pointer;font:inherit;font-size:.85rem}}
#npclose:hover{{color:var(--text);border-color:var(--muted)}}
/* list */
.day{{display:flex;align-items:center;gap:12px;margin:26px 0 12px;color:var(--muted);font-size:.78rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase}}
.day::after{{content:"";flex:1;height:1px;background:var(--line)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:18px}}
.v{{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;cursor:pointer;transition:transform .15s,border-color .15s,box-shadow .15s}}
.v:hover{{transform:translateY(-3px);border-color:#3a3f52;box-shadow:0 8px 22px rgba(0,0,0,.4)}}
.v.active{{border-color:var(--gold);box-shadow:0 0 0 1px var(--gold)}}
.tw{{position:relative;aspect-ratio:16/9;background:linear-gradient(135deg,#1c1f2a,#11131a)}}
.thumb{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block}}
.thumb-ph{{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#3a3f52;font-size:2.2rem}}
.play{{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.35);opacity:0;transition:opacity .15s}}
.play span{{width:54px;height:54px;border-radius:50%;background:var(--gold);color:#111;display:flex;align-items:center;justify-content:center;font-size:1.3rem;padding-left:4px;box-shadow:0 4px 14px rgba(0,0,0,.5)}}
.v:hover .play{{opacity:1}}
.dur{{position:absolute;right:8px;bottom:8px;background:rgba(0,0,0,.8);color:#fff;font-size:.75rem;font-weight:600;padding:2px 6px;border-radius:4px;font-variant-numeric:tabular-nums}}
.badge{{position:absolute;left:8px;top:8px;font-size:.68rem;font-weight:800;letter-spacing:.06em;text-transform:uppercase;padding:3px 7px;border-radius:4px}}
.live{{background:var(--red);color:#fff;animation:pulse 1.6s infinite}}
.cloud{{background:rgba(0,0,0,.7);color:#cfd6ff}}
@keyframes pulse{{50%{{opacity:.6}}}}
.vb{{display:flex;align-items:flex-start;gap:8px;padding:12px 14px 14px}}
.vinfo{{flex:1;min-width:0}}
.vtitle{{font-weight:700;font-size:.95rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.vmeta{{color:var(--muted);font-size:.8rem;margin-top:4px}}
.dl{{flex-shrink:0;display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;border-radius:8px;border:1px solid var(--line);background:none;color:var(--muted);cursor:pointer;font-size:.95rem;text-decoration:none}}
.dl:hover{{color:var(--gold);border-color:var(--gold);text-decoration:none}}
.empty{{text-align:center;padding:60px 20px;color:var(--muted);background:var(--card);border:1px dashed var(--line);border-radius:14px}}
.empty b{{display:block;color:var(--text);font-size:1.05rem;margin-bottom:4px}}
/* login modal */
#modal{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);align-items:center;justify-content:center;z-index:99}}
#modal.open{{display:flex}}
#mbox{{background:var(--card);border:1px solid var(--line);padding:24px;border-radius:14px;min-width:280px;text-align:center}}
#mbox h2{{margin:0 0 16px;font-size:1rem}}
#mbox input{{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);font-size:1rem}}
#mbox button{{margin-top:12px;width:100%;padding:10px;background:var(--gold);color:#111;border:0;border-radius:8px;font-size:1rem;cursor:pointer;font-weight:700}}
#merr{{color:var(--red);font-size:.85rem;margin-top:8px;min-height:1em}}
/* comments */
#cbox{{display:none;padding:16px 18px 18px}}
#cbox h2{{margin:0 0 10px;font-size:.95rem}}
.c{{padding:10px 0;border-bottom:1px solid var(--line)}}
.c:last-child{{border-bottom:0}}
.cn{{font-weight:700;margin-right:8px}}
.ct,.cpos{{color:var(--muted);font-size:.78rem;margin-right:8px}}
.cpos{{cursor:pointer;color:var(--gold);font-weight:600;font-variant-numeric:tabular-nums}}
.cdel{{background:none;border:0;color:var(--muted);cursor:pointer;font-size:.78rem;float:right}}
.cdel:hover{{color:var(--red)}}
.cx{{white-space:pre-wrap;word-wrap:break-word;margin-top:3px;line-height:1.45}}
#ctext{{width:100%;min-height:70px;margin-top:12px;padding:10px 12px;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:10px;font:inherit;resize:vertical}}
#ctext:focus{{outline:none;border-color:var(--gold)}}
#cpost{{margin-top:8px;padding:8px 18px;background:var(--gold);color:#111;border:0;border-radius:8px;font-weight:700;cursor:pointer;font:inherit;font-weight:700}}
#cpost:disabled{{opacity:.6}}
#cmsg{{color:var(--muted);font-size:.85rem;margin-top:8px}}
@media (max-width:520px){{body{{padding:16px 12px 32px}}.grid{{grid-template-columns:1fr 1fr;gap:12px}}.vb{{padding:10px}}.vtitle{{font-size:.85rem}}.dl{{display:none}}}}
{embed_css}
</style></head><body>
{header}
<div id="stage"><video id="p" controls playsinline></video>
<div class="np"><div><div class="np-k">Now playing</div><div id="nptitle"></div></div>
<button id="npclose" type="button">✕ Close</button></div>
{comments_box}</div>
{rows}
{login_modal}
<script>
var _stage=document.getElementById('stage'),_p=document.getElementById('p');
document.querySelectorAll('.v').forEach(r=>r.onclick=e=>{{
  if(e.target.tagName==='A'||e.target.closest('button'))return;
  var src=r.dataset.f,th=r.dataset.th||'';
  _stage.classList.add('on');
  document.querySelectorAll('.v.active').forEach(x=>x.classList.remove('active'));r.classList.add('active');
  document.getElementById('nptitle').textContent=r.dataset.t||'';
  if(th)_p.poster=th; else _p.removeAttribute('poster');
  _p.src=src;_p.play();scrollTo({{top:0,behavior:'smooth'}});
  if(window.loadComments)loadComments(r.dataset.n);
}});
document.getElementById('npclose').onclick=function(){{
  _p.pause();_p.removeAttribute('src');_p.load();_stage.classList.remove('on');
  document.querySelectorAll('.v.active').forEach(x=>x.classList.remove('active'));
}};
{login_js}
{comments_js}
</script></body></html>"""

COMMENTS_BOX = """<div id="cbox"><h2>Comments</h2><div id="clist"></div>
<div id="cform"><textarea id="ctext" maxlength="500" placeholder="Add a comment as your chat name…"></textarea>
<button id="cpost">Post</button></div><div id="cmsg"></div></div>"""

COMMENTS_JS = """
var _vod=null,_me=null,_admin=false;
var CERR={no_chat:'To comment, open the chat on the live page once so Owncast gives you a chat name, then come back.',
  banned:'You are banned from chat, so you can\\'t comment.',
  live:'Comments are closed while the stream is live. They reopen when it ends.',
  slow_down:'Slow down a little.',rate_limited:'Too many attempts. Try again later.',
  length:'Comments must be 1-500 characters.',full:'This VOD has reached its comment limit.',
  chat_unreachable:'Couldn\\'t reach chat to check your name. Try again later.'};
function chatToken(){try{var t=localStorage.getItem('accessToken')||'';
  try{var j=JSON.parse(t);if(typeof j==='string')t=j;}catch(e){}
  return /^[A-Za-z0-9_+\\/=.-]+$/.test(t)?t:'';}catch(e){return '';}}
function capi(url,body){var o={headers:{'X-Chat-Token':chatToken()},credentials:'same-origin'};
  if(body){o.method='POST';o.headers['Content-Type']='application/json';o.body=JSON.stringify(body);}
  return fetch(url,o).then(r=>r.json().then(d=>({status:r.status,d:d})));}
function fmt(s){s=Math.floor(s);var h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;
  return (h?h+':'+String(m).padStart(2,'0'):m)+':'+String(x).padStart(2,'0');}
function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e;}
function cmsg(t){document.getElementById('cmsg').textContent=t||'';}
function loadComments(name){_vod=name;document.getElementById('cbox').style.display='block';cmsg('');
  document.getElementById('clist').textContent='Loading…';
  capi('/vods/api/comments?vod='+encodeURIComponent(name)).then(function(res){
    if(name!==_vod)return;var d=res.d;_me=d.me||_me;_admin=!!d.admin;
    document.getElementById('cform').style.display=d.open?'':'none';
    if(!d.open)cmsg(CERR.live);
    var list=document.getElementById('clist');list.textContent='';
    if(!d.comments||!d.comments.length)list.appendChild(el('div','ct','No comments yet.'));
    (d.comments||[]).forEach(function(c){
      var row=el('div','c'),n=el('span','cn',c.name);
      n.style.color='hsl('+((c.color||0)*47%360)+',70%,70%)';row.appendChild(n);
      row.appendChild(el('span','ct',new Date(c.t*1000).toLocaleString()));
      if(c.pos!=null){var a=el('span','cpos','@'+fmt(c.pos));
        a.onclick=function(){var p=document.getElementById('p');p.currentTime=c.pos;p.play();scrollTo(0,0);};row.appendChild(a);}
      if(_admin||(_me&&(_me.mod||_me.id===c.uid))){var b=el('button','cdel','delete');
        b.onclick=function(){if(confirm('Delete this comment?'))capi('/vods/api/comments/delete',{vod:_vod,id:c.id})
          .then(function(r){if(r.d.ok)loadComments(_vod);else cmsg(CERR[r.d.error]||'Could not delete.');});};row.appendChild(b);}
      row.appendChild(el('div','cx',c.text));list.appendChild(row);});
  }).catch(function(){document.getElementById('clist').textContent='Could not load comments.';});}
document.getElementById('cpost').onclick=function(){
  var t=document.getElementById('ctext'),p=document.getElementById('p'),btn=this;
  if(!t.value.trim())return;btn.disabled=true;cmsg('Posting…');
  capi('/vods/api/comments',{vod:_vod,text:t.value,pos:p.currentTime||null}).then(function(r){
    btn.disabled=false;
    if(r.d.ok){t.value='';_me=r.d.me;loadComments(_vod);}
    else cmsg(CERR[r.d.error]||'Could not post.');
  }).catch(function(){btn.disabled=false;cmsg('Could not post.');});};"""

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
  .then(r=>r.json().then(d=>{
    if(r.status===429){document.getElementById('merr').textContent='Too many attempts. Try again later.';}
    else if(d.ok){document.getElementById('modal').classList.remove('open');
              document.getElementById('mpw').value='';
              if(_dlFile){var a=document.createElement('a');
                a.href=_dlCloud?'/vods/cloud/'+_dlFile+'?dl=1':'/vods/download/'+_dlFile;
                a.download=_dlFile;a.click();}
    }else{document.getElementById('merr').textContent='Wrong password.';}
  }));
}"""

OPEN_DOWNLOAD_JS = ""  # no modal needed when downloads are open

# ?embed=1: compact version for an iframe on the Owncast page (no title or
# back link, no page background, fills the frame's width)
EMBED_CSS = "body{background:transparent;padding:4px 2px 8px;max-width:none}.day:first-of-type{margin-top:4px}"


def _vod_start(v) -> float:
    """Start time of a VOD (local time zone) from its file name."""
    return time.mktime(time.strptime(f'{v["date"]} {v["start"]}', "%Y-%m-%d %H:%M"))


def _day_label(date: str) -> str:
    t = time.strptime(date, "%Y-%m-%d")
    today = time.strftime("%Y-%m-%d")
    yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    if date == today:
        return "Today"
    if date == yesterday:
        return "Yesterday"
    return f'{time.strftime("%A, %B", t)} {t.tm_mday}'


def _clock(hhmm: str) -> str:
    h, m = map(int, hhmm.split(":"))
    return f'{(h % 12) or 12}:{m:02d} {"AM" if h < 12 else "PM"}'


def _duration(v) -> str:
    """Approximate length of a finished local VOD: last write minus start."""
    if v.get("recording") or not v.get("mtime"):
        return ""
    s = int(v["mtime"] - _vod_start(v))
    if not 0 < s < 2 * 86400:
        return ""
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def render_page(authed: bool = False, embed: bool = False):
    """authed=True means the request already has a valid admin cookie."""
    vods = merged_vods()
    if not vods:
        rows = ('<div class="empty"><b>No past streams yet</b>'
                'Recordings show up here after each stream.</div>')
    else:
        rows, day = [], None
        gate = ADMIN_PASSWORD != ""
        for v in vods:
            if v["date"] != day:
                if day is not None:
                    rows.append("</div>")
                day = v["date"]
                rows.append(f'<div class="day">{_day_label(day)}</div><div class="grid">')
            f = html.escape(v["name"])
            is_cloud = v.get("cloud", False)
            has_thumb = v.get("thumb", False)

            # Thumbnail or placeholder
            if has_thumb:
                thumb_src = f"/vods/thumb/{f[:-4]}.jpg"
                thumb_html = f'<img class="thumb" src="{thumb_src}" alt="" loading="lazy">'
                data_th = f' data-th="{thumb_src}"'
            else:
                thumb_html = '<div class="thumb-ph">▶</div>'
                data_th = ""

            # Overlays: badge top-left, duration bottom-right
            if v.get("recording"):
                badge = '<span class="badge live">● Recording</span>'
            elif is_cloud:
                badge = '<span class="badge cloud">☁ Cloud</span>'
            else:
                badge = ""
            dur = _duration(v)
            dur_html = f'<span class="dur">{dur}</span>' if dur else ""

            # Player src (signed + expiring when a password is set)
            play = ("" if authed else _play_query(v["name"])).replace("&", "&amp;")
            player_src = (f"/vods/cloud/{f}" if is_cloud else f"/vods/{f}") + play

            # Download link/button
            if gate and not authed:
                dl = (f'<button class="dl" title="Download (admin)" '
                      f'onclick="openLogin(\'{f}\',{str(is_cloud).lower()})">🔒</button>')
            else:
                dl_href = f"/vods/cloud/{f}?dl=1" if is_cloud else f"/vods/download/{f}"
                dl = f'<a class="dl" href="{dl_href}" title="Download" download>⤓</a>'

            clock = _clock(v["start"])
            short_date = time.strftime("%b %d", time.strptime(v["date"], "%Y-%m-%d")).replace(" 0", " ")
            title = f"Stream · {clock}"
            rows.append(
                f'<div class="v" data-f="{player_src}" data-n="{f}" '
                f'data-t="{html.escape(f"{short_date} · {clock}", quote=True)}"{data_th}>'
                f'<div class="tw">{thumb_html}{badge}{dur_html}'
                f'<div class="play"><span>▶</span></div></div>'
                f'<div class="vb"><div class="vinfo">'
                f'<div class="vtitle">{title}</div>'
                f'<div class="vmeta">{short_date} · {human(v["size"])}</div>'
                f'</div>{dl}</div>'
                f'</div>'
            )
        rows.append("</div>")
        rows = "\n".join(rows)

    if ADMIN_PASSWORD and not authed:
        modal = LOGIN_MODAL
        js = LOGIN_JS
    else:
        modal = ""
        js = OPEN_DOWNLOAD_JS

    return PAGE.format(
        title=html.escape(TITLE),
        header="" if embed else (f'<div class="top"><div><h1>{html.escape(TITLE)}</h1>'
                                 f'<div class="sub">{len(vods)} recording{"" if len(vods) == 1 else "s"}</div></div></div>'),
        embed_css=EMBED_CSS if embed else "",
        rows=rows,
        login_modal=modal,
        login_js=js,
        comments_box=COMMENTS_BOX if COMMENTS else "",
        comments_js=COMMENTS_JS if COMMENTS else "",
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
            embed = "embed" in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            # no-store: the page holds expiring playback links
            return self._send(200, render_page(authed, embed), "text/html; charset=utf-8",
                              extra={"Cache-Control": "no-store"})
        if path in ("/", ""):
            return self._send(302, extra={"Location": "/vods"})
        if path == "/vods/api/list":
            return self._send(200, json.dumps(merged_vods()).encode(), "application/json")
        if path == "/vods/api/comments" and COMMENTS:
            name = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("vod", [""])[0]
            if not vod_exists(name):
                return self._json(404, {"error": "not_found"})
            # Who's viewing, from the cache only: never opens a chat connection.
            token = self._chat_token()
            with _ident_lock:
                hit = _cached_identity(token) if token else None
            me = _public_user(hit[1]) if hit and hit[0] == "ok" else None
            return self._json(200, {"comments": load_comments(name), "open": not rec.online,
                                    "me": me, "admin": _is_admin(self)},
                              extra={"Cache-Control": "no-store"})
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
            if not _play_allowed(self, name):
                return self._send(403, b"link expired - reload the VOD page")
            return self.proxy_cloud(name, download=force_dl)
        # /vods/thumb/<name>.jpg  — VOD thumbnail (public)
        if path.startswith("/vods/thumb/"):
            return self.serve_thumb(urllib.parse.unquote(path[len("/vods/thumb/"):]))
        # /vods/<name>  — streaming (needs a signed link from the page if a password is set)
        if path.startswith("/vods/"):
            name = urllib.parse.unquote(path[6:])
            if not _play_allowed(self, name):
                return self._send(403, b"link expired - reload the VOD page")
            return self.serve_file(name)
        self._send(404, b"not found")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        # ---- Admin login ----
        if u.path == "/vods/login":
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(min(n, 4096)) or b"{}")
                pw = str(body.get("password", ""))
            except (ValueError, AttributeError):
                pw = ""
            ip = _client_ip(self)
            if _login_blocked(ip):
                log("login blocked (too many attempts) from", ip)
                return self._send(429, json.dumps({"ok": False}).encode(), "application/json",
                                  extra={"Retry-After": str(LOGIN_WINDOW)})
            if ADMIN_PASSWORD and hmac.compare_digest(_hash(pw), _hash(ADMIN_PASSWORD)):
                token = _make_token()
                resp = json.dumps({"ok": True}).encode()
                # HttpOnly so JS can't steal it; SameSite=Strict; no Secure flag
                # (the reverse proxy handles HTTPS termination)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.send_header("Set-Cookie",
                    f"vod_admin={token}; Path=/vods; HttpOnly; SameSite=Strict; "
                    f"Max-Age={SESSION_SECONDS}")
                self.end_headers()
                self.wfile.write(resp)
            else:
                _login_failed(ip)
                log("failed login from", ip)
                time.sleep(1)  # slow down guessing even below the limit
                self._send(401, json.dumps({"ok": False}).encode(), "application/json")
            return
        if u.path in ("/vods/api/comments", "/vods/api/comments/delete") and COMMENTS:
            try:
                if u.path.endswith("/delete"):
                    return self.delete_comment()
                return self.post_comment()
            except CommentError as e:
                return self._json(e.code, {"ok": False, "error": e.err})
        # ---- Owncast webhook: STREAM_STARTED / STREAM_STOPPED ----
        if u.path not in ("/webhook", "/vods/webhook"):
            return self._send(404, b"not found")
        token = urllib.parse.parse_qs(u.query).get("token", [""])[0]
        if WEBHOOK_TOKEN and not hmac.compare_digest(token.encode(), WEBHOOK_TOKEN.encode()):
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

    def _json(self, code, obj, extra=None):
        self._send(code, json.dumps(obj).encode(), "application/json", extra)

    def _json_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(min(n, 16384)) or b"{}")
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def _chat_token(self) -> str:
        """Owncast chat token: its cookie (v0.3.0+), else the page's X-Chat-Token
        header (copied from localStorage on older Owncast)."""
        tok = self.headers.get("X-Chat-Token", "").strip()
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "owncast_chat_token" and v.strip():
                tok = v.strip().strip('"')
                break
        return tok if TOKEN_RE.match(tok) else ""

    def post_comment(self):
        body = self._json_body()
        name = str(body.get("vod", ""))
        text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(body.get("text", ""))).strip()
        if not vod_exists(name):
            raise CommentError(404, "not_found")
        if not text or len(text) > COMMENT_MAX_LEN:
            raise CommentError(400, "length")
        if rec.stream_online():  # fresh check, not the last poll
            raise CommentError(409, "live")
        token = self._chat_token()
        if not token:
            raise CommentError(401, "no_chat")
        user = identify(token, _client_ip(self))
        pos = body.get("pos")
        pos = round(pos, 1) if isinstance(pos, (int, float)) and 0 <= pos < 1e6 else None
        with _comment_lock:
            now = time.time()
            if now - _last_comment.get(user.get("id"), 0) < COMMENT_MIN_GAP:
                raise CommentError(429, "slow_down")
            items = load_comments(name)
            if len(items) >= COMMENT_MAX_PER_VOD:
                raise CommentError(409, "full")
            c = {"id": secrets.token_hex(6), "uid": user.get("id"),
                 "name": str(user.get("displayName") or "?")[:60],
                 "color": user.get("displayColor") if isinstance(user.get("displayColor"), int) else 0,
                 "text": text, "t": int(now), "pos": pos}
            items.append(c)
            try:
                save_comments(name, items)
            except OSError as e:
                log("could not save comment for", name, e)
                raise CommentError(500, "save_failed")
            _last_comment[user.get("id")] = now
        log("comment on", name, "by", c["name"])
        self._json(200, {"ok": True, "comment": c, "me": _public_user(user)})

    def delete_comment(self):
        """Admin, Owncast moderators, and the comment's author may delete it."""
        body = self._json_body()
        name, cid = str(body.get("vod", "")), str(body.get("id", ""))
        if not FILE_RE.match(name):
            raise CommentError(404, "not_found")
        user = None
        if not _is_admin(self):
            token = self._chat_token()
            if not token:
                raise CommentError(401, "no_chat")
            user = identify(token, _client_ip(self))
        with _comment_lock:
            items = load_comments(name)
            c = next((c for c in items if c.get("id") == cid), None)
            if c is None:
                raise CommentError(404, "not_found")
            if user is not None and not _is_mod(user) and c.get("uid") != user.get("id"):
                raise CommentError(403, "forbidden")
            items.remove(c)
            save_comments(name, items)
        log("comment deleted on", name, "by", user.get("displayName") if user else "admin")
        self._json(200, {"ok": True})

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


    def serve_thumb(self, name):
        """Serve a VOD thumbnail JPEG. name is the .jpg filename."""
        # Accept either vod_*.jpg directly or vod_*.mp4 (derive jpg name)
        if name.endswith(".mp4"):
            name = name[:-4] + ".jpg"
        if not THUMB_RE.match(name):
            return self._send(404, b"not found")
        p = VOD_DIR / name
        if not p.is_file():
            # VOD deleted locally? The backup also uploads thumbnails.
            if CLOUD_URL:
                return self._proxy(name, cache="public, max-age=86400")
            return self._send(404, b"not found")
        data = p.read_bytes()
        self._send(200, data, "image/jpeg",
                   extra={"Cache-Control": "public, max-age=86400"})

    def proxy_cloud(self, name, download=False):
        """Proxy a cloud VOD from rclone WebDAV, with Range passthrough.
        download=True adds Content-Disposition: attachment (saves the file).
        Without it the browser can play the video inline in the page player.
        """
        if not FILE_RE.match(name):
            return self._send(404, b"not found")
        return self._proxy(name, download=download)

    def _proxy(self, name, download=False, cache=None):
        """Stream an already-validated file name from the rclone WebDAV server."""
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
        if cache:
            self.send_header("Cache-Control", cache)
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
    if WEBHOOK_TOKEN.startswith("change-me") or ADMIN_PASSWORD.startswith("change-me"):
        log("WARNING: WEBHOOK_TOKEN / ADMIN_PASSWORD still use the example value - change them in .env")
    if SMTP_HOST and not NOTIFY_URL.startswith(("http://", "https://")):
        log("WARNING: NOTIFY_URL should be a full URL (https://...) or email links won't work")
    threading.Thread(target=rec.loop, daemon=True).start()

    def shutdown(*_):
        rec.stop()
        sys.exit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
