"""Point Owncast's webhook at the recorder, with the events it uses.

Run on the Owncast server:  python3 owncast-webhook.py [https://your.site]
  - STREAM_STARTED / STREAM_STOPPED: start and stop recording right away
  - CHAT / VISIBILITY-UPDATE: chat replay (and hiding moderated messages)
Reads WEBHOOK_TOKEN from .env next to this file. Owncast can't edit a
webhook's events, so an existing /vods/webhook entry is replaced. Other
webhooks are left alone.
"""
import base64, getpass, json, os, sys, urllib.parse, urllib.request

OC = os.environ.get("OWNCAST_ADMIN_URL", "http://127.0.0.1:8080")
EVENTS = ["STREAM_STARTED", "STREAM_STOPPED", "CHAT", "VISIBILITY-UPDATE"]
HERE = os.path.dirname(os.path.abspath(__file__))

env = {}
for line in open(os.path.join(HERE, ".env"), encoding="utf-8"):
    k, sep, v = line.strip().partition("=")
    if sep and not k.startswith("#"):
        env[k.strip()] = v.strip().strip('"').strip("'")
token = env.get("WEBHOOK_TOKEN", "")
if not token or token.startswith("change-me"):
    raise SystemExit("set WEBHOOK_TOKEN in .env first (openssl rand -hex 16)")

auth = "Basic " + base64.b64encode(b"admin:" + getpass.getpass("Owncast admin password: ").encode()).decode()


def api(path, body=None):
    req = urllib.request.Request(OC + "/api/admin/" + path,
                                 None if body is None else json.dumps(body).encode(),
                                 {"Authorization": auth, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


old = [h for h in api("webhooks") if "/vods/webhook" in h.get("url", "")]
if len(sys.argv) > 1:
    site = sys.argv[1].rstrip("/")
elif old:
    u = urllib.parse.urlsplit(old[0]["url"])
    site = f"{u.scheme}://{u.netloc}"
else:
    raise SystemExit("no /vods webhook yet - run: python3 owncast-webhook.py https://your.site")

url = f"{site}/vods/webhook?token={token}"
if any(h["url"] == url and sorted(h.get("events") or []) == sorted(EVENTS) for h in old) and len(old) == 1:
    print("webhook already set up:", site + "/vods/webhook")
    raise SystemExit(0)
api("webhooks/create", {"url": url, "events": EVENTS})
for h in old:
    api("webhooks/delete", {"id": h["id"]})
print("webhook:", site + "/vods/webhook", "->", ", ".join(EVENTS))
