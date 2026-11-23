# owncast-vods

Automatic VODs for [Owncast](https://owncast.online). Records your stream into
fixed-length MP4 files (4 hours by default) and serves them on a `/vods` page
next to your stream, with optional backup to Dropbox, Google Drive, S3, B2 or
any other [rclone](https://rclone.org) storage.

Made for 24/7 streams, but it works for regular streams too.

- **Clock-aligned chunks.** Files split at fixed times (with 4-hour chunks:
  00:00, 04:00, 08:00 ...) in your time zone. Length is configurable.
- **Lossless and light.** The stream is copied, not re-encoded, so CPU use is
  close to zero.
- **Crash-safe.** Fragmented MP4: the current chunk can be watched while it
  records, and a crash never corrupts a file.
- **Follows the stream.** Recording starts when you go live and stops when you
  go offline. With the Owncast webhook, it reacts instantly; without it,
  within 15 seconds.
- **VOD page.** Grouped by day, with a built-in player, seeking, downloads and a
  JSON API. Add it to Owncast as a button for viewers.
- **Manages disk space.** Deletes old VODs after N days, or sooner when the
  disk runs low.
- **Cloud backup.** Optional: copies every finished VOD to the cloud.
- **Owncast is not modified.**

```
OBS ──RTMP──► Owncast ──HLS──► recorder ──► vods/*.mp4 ──► (optional) rclone ──► cloud
                 ▲                ▲
                 │ /              │ /vods
          your reverse proxy ◄──── https://stream.example.com
```

---

## Contents

1. [Requirements](#1-requirements)
2. [Install](#2-install)
3. [Find your Owncast URL](#3-find-your-owncast-url)
4. [Start](#4-start)
5. [Add /vods to your reverse proxy](#5-add-vods-to-your-reverse-proxy)
6. [Connect to Owncast](#6-connect-to-owncast)
7. [Cloud backup (optional)](#7-cloud-backup-optional)
8. [Settings](#8-settings)
9. [Everyday commands](#9-everyday-commands)
10. [Troubleshooting](#10-troubleshooting)
11. [Uninstall](#11-uninstall)

**Quick start**, if you already know your way around:

```bash
git clone https://github.com/kennethyork/Owncast-vods.git /opt/owncast-vods
cd /opt/owncast-vods && cp .env.example .env && nano .env
docker compose up -d --build
# then proxy /vods -> 127.0.0.1:8090 (examples in proxy/)
```

---

## 1. Requirements

- A **Linux** server with **Docker** and the **Compose plugin**
  (`docker compose version` should work).
- **Owncast** that the server can reach over HTTP. It can run in Docker, as a
  plain install, or on another machine.
- A **reverse proxy** in front of Owncast (nginx, Caddy, CloudPanel, Nginx
  Proxy Manager, Cloudflare Tunnel ...) so `/vods` can share your domain.
- **Disk space.** Each hour takes about **0.45 GB per Mbps** of stream bitrate:

  | Stream bitrate | Per 4-hour VOD | Per day (24/7) | 14 days |
  |---|---|---|---|
  | 2.5 Mbps | ~4.5 GB | ~27 GB | ~380 GB |
  | 4 Mbps | ~7.2 GB | ~43 GB | ~600 GB |
  | 6 Mbps | ~10.8 GB | ~65 GB | ~900 GB |
  | 8 Mbps | ~14.4 GB | ~86 GB | ~1.2 TB |

  Set `RETENTION_DAYS` to match the space you have.

---

## 2. Install

```bash
git clone https://github.com/kennethyork/Owncast-vods.git /opt/owncast-vods
cd /opt/owncast-vods
cp .env.example .env
nano .env
```

At minimum, set these in `.env`:

| Setting | Set it to |
|---|---|
| `OWNCAST_URL` | Owncast's address (see step 3) |
| `TZ` | Your time zone, e.g. `America/New_York`, `Europe/London` |
| `WEBHOOK_TOKEN` | Any random text, e.g. from `openssl rand -hex 16` |
| `ADMIN_PASSWORD` | Password to gate downloads (leave empty to allow everyone) |
| `RETENTION_DAYS` | Days of VODs to keep (see the disk table) |

All other settings are explained in [Settings](#8-settings). `.env` isn't
tracked by git, so `git pull` never overwrites it.

---

## 3. Find your Owncast URL

**Owncast in Docker on the same server:**

```bash
docker ps --format "table {{.Names}}\t{{.Ports}}"
```

On the Owncast line, the number before `->8080` is the host port, e.g.
`0.0.0.0:8080->8080/tcp` → `OWNCAST_URL=http://127.0.0.1:8080`.
(No `->` in the output? See [Owncast on a Docker network](#owncast-on-a-docker-network).)

**Owncast installed directly on the same server:** the default is
`http://127.0.0.1:8080` (Owncast's web server port in its admin settings).

**Owncast on another machine:** use its public URL, e.g.
`OWNCAST_URL=https://stream.example.com`. This works, but the recorder then
downloads the whole stream over the internet.

Test it:

```bash
curl -s http://127.0.0.1:8080/api/status
```

You should get JSON containing `"online":true` or `"online":false`.

---

## 4. Start

```bash
cd /opt/owncast-vods
docker compose up -d --build
docker compose logs -f vods
```

While you're live, you should see:

```
owncast-vod: http://127.0.0.1:8080, chunks=14400s, dir=/vods, port=8090
recording http://127.0.0.1:8080/hls/stream.m3u8 in 14400 s chunks
```

Press `Ctrl+C` to stop following the logs (the recorder keeps running). Check
the page and the files:

```bash
curl -I http://127.0.0.1:8090/vods    # 200 OK
ls -lh vods/                          # vod_YYYY-MM-DD_HHMMSS.mp4, growing
```

The container restarts on its own after a reboot or a crash.

---

## 5. Add /vods to your reverse proxy

The recorder listens on `127.0.0.1:8090`. Send `/vods` on your Owncast domain
there and everything else to Owncast, as usual. Ready-to-use snippets are in
[`proxy/`](proxy/).

### nginx

Add [`proxy/nginx.conf`](proxy/nginx.conf) inside your Owncast `server { }`
block (the `listen 443` one), **above** `location / {`:

```nginx
  location /vods {
    proxy_pass http://127.0.0.1:8090;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 3600s;
  }
```

Then run `nginx -t && systemctl reload nginx`.

### CloudPanel

Sites → your Owncast site → **Vhost** tab. Paste the nginx block above inside
the `listen 443` server block, above `location / {`, and click **Save**.
CloudPanel validates the config before applying it.

The same works for other panels that let you edit the nginx vhost (Plesk
"Additional nginx directives", aaPanel, HestiaCP custom templates).

### Caddy

See [`proxy/Caddyfile`](proxy/Caddyfile):

```caddy
stream.example.com {
	handle /vods* {
		reverse_proxy 127.0.0.1:8090
	}
	handle {
		reverse_proxy 127.0.0.1:8080
	}
}
```

### Cloudflare Tunnel

Add a `path: ^/vods` rule **above** your Owncast rule. See
[`proxy/cloudflared.yml`](proxy/cloudflared.yml). For large files, also add a
Cloudflare cache rule that bypasses `/vods/*`.

### Nginx Proxy Manager (or any proxy running in a container)

A proxy inside a container can't reach the host's `127.0.0.1`. In `.env`, set
`BIND=0.0.0.0`, block port 8090 from the internet in your firewall
(`ufw deny 8090`), then in NPM → your Owncast host → **Custom locations**, add
`/vods` → forward to your server's IP (or `172.17.0.1`), port `8090`.

### Separate subdomain instead

Point e.g. `vods.example.com` at `127.0.0.1:8090` as a normal proxy host. The
page is at `vods.example.com/vods`, and `/` redirects there. Set
`LIVE_URL=https://stream.example.com` so "back to live" works.

When it's set up, open **`https://stream.example.com/vods`**.

---

## 6. Connect to Owncast

In the Owncast admin (`/admin`) → **Integrations**:

### VODs button for viewers (External Action)

**External Actions → Create new**
- **URL:** `https://stream.example.com/vods`
- **Title:** `VODs`
- **Open in new tab:** off shows the page as a pop-up over the stream; on
  opens the full page.

### Instant start/stop (Webhook, recommended)

**Webhooks → Create webhook**
- **URL:** `https://stream.example.com/vods/webhook?token=YOUR_WEBHOOK_TOKEN`
- **Events:** **Stream Started** and **Stream Stopped**

Test it: stop and restart your broadcast, then run `docker compose logs vods`
and look for `webhook STREAM_STOPPED` / `webhook STREAM_STARTED`.

---

## 7. Cloud backup (optional)

A second container uses [rclone](https://rclone.org) to copy every
**finished** VOD to cloud storage. It runs every 15 minutes and skips the file
still being recorded. It only **copies**: VODs deleted locally by
`RETENTION_DAYS` stay in the cloud. Use `BACKUP_RETENTION_DAYS` to clean the
cloud side too.

rclone supports 70+ providers. Common ones:

| Provider | rclone type | Notes |
|---|---|---|
| Dropbox | `dropbox` | Walkthrough below |
| Google Drive | `drive` | Same browser-token flow as Dropbox |
| OneDrive | `onedrive` | Same browser-token flow |
| Backblaze B2 | `b2` | Key ID + application key, no browser needed |
| Amazon S3 / Wasabi / R2 / MinIO | `s3` | Access key + secret, no browser needed |
| Another server | `sftp` | Host, user, key/password |

### 7a. Create the rclone remote

```bash
cd /opt/owncast-vods
docker run --rm -it -v "$PWD/rclone:/config/rclone" rclone/rclone config
```

Choose `n` (new remote), give it a name (this is `BACKUP_REMOTE` in `.env`),
and pick your provider. For key-based providers (S3, B2, SFTP), paste your
keys and you're done.

#### Dropbox, Google Drive, OneDrive (browser login)

The server has no browser, so you log in once from your own computer:

1. On your PC/Mac, install rclone (<https://rclone.org/downloads/>) and run:
   ```bash
   rclone authorize "dropbox"        # or "drive" / "onedrive"
   ```
2. Log in and click **Allow**. The terminal prints a token
   (`{"access_token":...}`). Copy the whole line.
3. On the server, in `rclone config`:

   | Prompt | Answer |
   |---|---|
   | `n/s/q>` | `n` |
   | `name>` | `dropbox` |
   | `Storage>` | `dropbox` |
   | `client_id>` / `client_secret>` | Enter (blank) |
   | `Edit advanced config?` | `n` |
   | `Use web browser to automatically authenticate?` | `n` |
   | `config_token>` | paste the token |
   | `Keep this remote?` | `y`, then `q` |

Check that it works (this should list your folders):

```bash
docker run --rm -v "$PWD/rclone:/config/rclone" rclone/rclone lsd dropbox:
```

### 7b. Turn on the backup

In `.env`:

```bash
COMPOSE_PROFILES=backup
BACKUP_REMOTE=dropbox          # the name from 7a
BACKUP_FOLDER=Owncast VODs     # S3/B2: bucket-name/folder
BACKUP_RETENTION_DAYS=0        # e.g. 30 to stop the cloud filling up
```

Then:

```bash
docker compose up -d
docker compose logs -f backup
```

You'll see `cloud backup: /vods -> dropbox:Owncast VODs every 15 min`. The
first upload happens once the current chunk finishes.

---

## 8. Settings

All in `.env`. After changing anything, run `docker compose up -d`.

| Setting | Default | What it does |
|---|---|---|
| `OWNCAST_URL` | `http://127.0.0.1:8080` | Where the recorder reaches Owncast |
| `HLS_PATH` | `/hls/stream.m3u8` | `/hls/0/stream.m3u8` records one specific quality instead of the best |
| `SEGMENT_SECONDS` | `14400` | VOD length: 3600 = 1 h, 7200 = 2 h, 21600 = 6 h |
| `TZ` | `UTC` | Time zone for split times and file names |
| `VOD_PATH` | `./vods` | Folder on the host for VOD files |
| `RETENTION_DAYS` | `14` | Delete local VODs after N days (`0` = never) |
| `MIN_FREE_GB` | `5` | Delete the oldest VODs when free space drops below this |
| `TITLE` | `Stream VODs` | Heading on the VOD page |
| `LIVE_URL` | `/` | Where "back to live" links |
| `PORT` | `8090` | VOD page port |
| `BIND` | `127.0.0.1` | `0.0.0.0` to listen on all interfaces |
| `WEBHOOK_TOKEN` | – | Required `?token=` on the webhook URL |
| `ADMIN_PASSWORD` | – | Password to download VODs (empty = no gate) |
| `COMPOSE_PROFILES` | – | `backup` turns on cloud backup |
| `BACKUP_REMOTE` | `dropbox` | rclone remote name |
| `BACKUP_FOLDER` | `Owncast VODs` | Folder (or bucket/folder) on the remote |
| `BACKUP_INTERVAL_MIN` | `15` | Minutes between upload runs |
| `BACKUP_RETENTION_DAYS` | `0` | Delete from the remote after N days (`0` = never) |

**Endpoints:** `/vods` (page), `/vods/<file>.mp4` (video stream, no auth),
`/vods/download/<file>.mp4` (download, gated by `ADMIN_PASSWORD`),
`/vods/login` (POST, sets session cookie), `/vods/api/list` (JSON),
`/vods/webhook` (Owncast webhook).

### Admin downloads

Set `ADMIN_PASSWORD` in `.env` to protect downloads (e.g. for uploading to YouTube)
while keeping the VOD page and in-browser playback open to everyone:

```env
ADMIN_PASSWORD=supersecretpassword
```

After setting it, the download link on `/vods` becomes a 🔒 button.
Click it → enter the password → the browser downloads the file.
The session lasts 24 hours (one cookie, no database needed).
Leave `ADMIN_PASSWORD` empty to allow anyone to download.

Generate a strong password:
```bash
openssl rand -hex 20
```

---

## 9. Everyday commands

Run these in `/opt/owncast-vods`:

| Task | Command |
|---|---|
| Recorder log | `docker compose logs -f vods` |
| Backup log | `docker compose logs -f backup` |
| Restart | `docker compose restart` |
| Stop | `docker compose down` |
| Apply `.env` changes | `docker compose up -d` |
| Update to latest | `git pull && docker compose up -d --build` |
| Disk usage | `du -sh vods && df -h .` |

---

## 10. Troubleshooting

**`/vods` gives 502 Bad Gateway**
The recorder isn't running or isn't reachable. Check `docker compose ps` and
`curl -I http://127.0.0.1:8090/vods`. If your proxy runs in a container, see
the Nginx Proxy Manager section.

**`/vods` shows Owncast's page or Owncast's 404**
The proxy rule isn't matching. In nginx, the block must be inside the right
`server { }` and above `location / {`. In Cloudflare Tunnel, the `/vods` rule
must come first.

**"No VODs yet" while live**
Run `docker compose logs vods`:
- `status check failed ... Connection refused`: `OWNCAST_URL` is wrong. Redo
  step 3.
- `recording ...` and then ffmpeg errors: check that
  `curl -s $OWNCAST_URL/hls/stream.m3u8` prints a playlist.

**Can't skip ahead in a video**
Make sure `proxy_buffering off;` is set (nginx). Behind Cloudflare, bypass the
cache for `/vods/*`.

**Split times are off by hours**
Set `TZ` in `.env`, then run `docker compose up -d`.

<a id="owncast-on-a-docker-network"></a>
**Owncast on a Docker network (no host port)**
Either publish it (add `ports: ["127.0.0.1:8080:8080"]` to Owncast's compose
file and recreate it), or put the recorder on Owncast's network. Create
`docker-compose.override.yml`:

```yaml
services:
  vods:
    network_mode: !reset null
    ports: ["127.0.0.1:8090:8090"]
    environment:
      BIND: 0.0.0.0
    networks: [owncast]
networks:
  owncast:
    external: true
    name: owncast_default     # find it with: docker network ls
```

Then set `OWNCAST_URL=http://<owncast-container-name>:8080` and run
`docker compose up -d`.

**Webhook does nothing**
The token must match `WEBHOOK_TOKEN` exactly, and the URL must go through
your proxy to `/vods/webhook`. Even without the webhook, recording still
follows the stream within 15 seconds.

**Backup log: "remote ... is not set up yet"**
Redo 7a. The remote name must equal `BACKUP_REMOTE`, and you must run the
command from the repo folder (it saves to `./rclone`).

**Backup: token expired / auth error (Dropbox, Drive, OneDrive)**
Run `rclone authorize "dropbox"` on your computer again. Then, on the server:
`docker run --rm -it -v "$PWD/rclone:/config/rclone" rclone/rclone config reconnect dropbox:`
and paste the new token. Finally, run `docker compose restart backup`.

---

## 11. Uninstall

```bash
cd /opt/owncast-vods && docker compose --profile backup down
# remove the /vods proxy rule, and the External Action + webhook in Owncast
rm -rf /opt/owncast-vods      # WARNING: also deletes all local VODs
```

---

## Notes

- The recorder reads Owncast's public HLS output, so it records what viewers
  see, from the highest-quality variant by default.
- When a broadcast ends, Owncast appends a short "offline" clip to the stream,
  and this can end up at the end of the last VOD.
- If the stream drops and comes back, a new file starts at that moment. Later
  splits stay on the clock grid.

## License

MIT. See [LICENSE](LICENSE).
