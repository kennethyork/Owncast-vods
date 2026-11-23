# owncast-vod

Records a 24/7 Owncast stream into 4-hour VODs and serves them on a `/vods` page.

**For:** Owncast running in Docker, behind CloudPanel, at `live.kennethyork.com`
**Result:** your 24/7 stream is saved as one MP4 every 4 hours and listed at
**https://live.kennethyork.com/vods**

---

## How it works

```
OBS ──RTMP──► Owncast (Docker) ──HLS──► owncast-vod (Docker) ──► ./vods/*.mp4
                    ▲                          ▲
                    │ /                        │ /vods
              CloudPanel nginx ◄──── https://live.kennethyork.com
```

- A small container reads Owncast's live HLS stream and copies it to disk
  without re-encoding: no quality loss and almost no CPU.
- Files split on the clock at **12am, 4am, 8am, 12pm, 4pm and 8pm** (Eastern).
- Fragmented MP4: the current chunk can be watched while it records, and a
  crash never corrupts a file.
- If the stream drops, recording pauses. When it comes back, a new file starts
  and the 4-hour schedule is kept.
- Old VODs are deleted automatically (14 days by default, or sooner if the disk
  gets low).
- Optional **Dropbox backup** of every finished VOD.
- Owncast itself is **not changed**.

---

## 1. Requirements

- SSH access to the server as **root** (or a user that can run `docker`)
- Docker with the Compose plugin (already there if Owncast runs in Docker).
  Check with:
  ```bash
  docker compose version
  ```
- Disk space. Each 4-hour file takes about **1.8 GB per Mbps** of stream bitrate:

  | Stream bitrate | Per 4-hour VOD | Per day | 14 days |
  |---|---|---|---|
  | 3 Mbps | ~5.4 GB | ~32 GB | ~450 GB |
  | 6 Mbps | ~10.8 GB | ~65 GB | ~900 GB |
  | 8 Mbps | ~14.4 GB | ~86 GB | ~1.2 TB |

  If that's more space than you have, lower `RETENTION_DAYS` in step 3.

---

## 2. Find Owncast's port

SSH into the server and run:

```bash
docker ps --format "table {{.Names}}\t{{.Ports}}"
```

Look at the Owncast line, for example:

```
owncast   0.0.0.0:8080->8080/tcp, 0.0.0.0:1935->1935/tcp
```

The number **before** `->8080` is the host port, here `8080`. Your CloudPanel
site uses the same one: Sites → live.kennethyork.com → Settings →
**Reverse Proxy URL** (for example `http://127.0.0.1:8080`).

Check that Owncast answers on it:

```bash
curl -s http://127.0.0.1:8080/api/status
```

You should see JSON with `"online":true` or `"online":false`. Note the port;
you'll need it in step 3.

> If Owncast has **no** host port (no `->8080` in the output), see
> *Troubleshooting → Owncast has no host port* at the end.

---

## 3. Clone and configure

```bash
cd /opt
git clone https://github.com/kennethyork/Owncast-vods.git owncast-vod
cd owncast-vod
cp .env.example .env
nano .env
```

In `.env`, set:
- `OWNCAST_URL` → the port from step 2 (default `http://127.0.0.1:8080`)
- `WEBHOOK_TOKEN` → any random text (used in step 6)
- `RETENTION_DAYS` → how many days of VODs to keep (see the disk table above)
- `VOD_PATH` → where to save files (default `./vods`, for example `/mnt/storage/vods`)

Save with `Ctrl+O`, `Enter`, `Ctrl+X`.

---

## 4. Start the recorder

```bash
cd /opt/owncast-vod
docker compose up -d --build
docker compose logs -f
```

If your stream is live, you should see:

```
owncast-vod: http://127.0.0.1:8080, chunks=14400s, dir=/vods, port=8090
recording http://127.0.0.1:8080/hls/stream.m3u8 in 14400 s chunks
```

Press `Ctrl+C` to stop following the logs (the recorder keeps running). Check
that it's serving the page:

```bash
curl -I http://127.0.0.1:8090/vods      # should say 200 OK
ls -lh /opt/owncast-vod/vods            # a vod_....mp4 file appears and grows
```

The container restarts on its own after a reboot or a crash
(`restart: unless-stopped`).

---

## 5. Add the `/vods` page in CloudPanel

1. Log in to CloudPanel → **Sites** → **live.kennethyork.com** → **Vhost** tab.
2. Find the `server {` block that has `listen 443`. Inside it, find the
   existing `location / {` block.
3. Paste this **above** `location / {` (also in `nginx/cloudpanel-vhost.conf`):

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

4. Click **Save**. CloudPanel checks the config and rejects the change if
   there's a mistake, so your site won't go down.
5. Open **https://live.kennethyork.com/vods**. You'll see the list of VODs
   grouped by day, with a player and a download link for each.

---

## 6. Connect it to Owncast (admin panel)

Open `https://live.kennethyork.com/admin` → **Integrations**.

### VODs button for viewers (External Action)
**Integrations → External Actions → Create new**
- **URL:** `https://live.kennethyork.com/vods`
- **Action title:** `VODs`
- **Description:** `Watch past broadcasts` (optional)
- Leave **Open in new tab** off to show it as a pop-up over the stream, or
  turn it on to open the full page.

### Instant start/stop (Webhook, optional but recommended)
Without this, the recorder checks Owncast every 15 seconds. With it, recording
starts and stops right away.

**Integrations → Webhooks → Create webhook**
- **URL:** `https://live.kennethyork.com/vods/webhook?token=YOUR_WEBHOOK_TOKEN`
  (use the same text you set in `.env`)
- **Events:** tick **Stream Started** and **Stream Stopped** only

Test it: stop and restart OBS, then run `docker compose logs` and look for
`webhook STREAM_STOPPED` / `webhook STREAM_STARTED`.

---

## 7. Dropbox backup (optional)

A second container uploads every **finished** VOD to your Dropbox. It runs
every 15 minutes and skips the file that's still recording. It only
**copies**, so VODs the VPS deletes after `RETENTION_DAYS` stay in Dropbox.

**Plan for space:** at 6 Mbps a day of VODs is about 65 GB, so a 2 TB Dropbox
fills in about a month. Set `DROPBOX_RETENTION_DAYS` (for example `25`) to keep
it from filling up.

### 7a. Get a Dropbox token (on your own computer)

The VPS has no browser, so you log in to Dropbox from your PC or Mac once:

1. Install rclone from <https://rclone.org/downloads/>.
2. In a terminal, run:
   ```bash
   rclone authorize "dropbox"
   ```
3. A browser opens. Log in to Dropbox and click **Allow**.
4. The terminal prints a token that starts with `{"access_token":...}`.
   Copy the whole line, including the `{` and `}`.

### 7b. Give the token to the VPS

On the VPS:

```bash
cd /opt/owncast-vod
docker run --rm -it -v "$PWD/rclone:/config/rclone" rclone/rclone config
```

Answer the prompts:

| Prompt | Type |
|---|---|
| `n/s/q>` | `n` (new remote) |
| `name>` | `dropbox` |
| `Storage>` | `dropbox` |
| `client_id>` / `client_secret>` | press Enter (leave blank) |
| `Edit advanced config?` | `n` |
| `Use web browser to automatically authenticate?` | `n` |
| `config_token>` | paste the token from 7a |
| `Keep this "dropbox" remote?` | `y` |
| `e/n/d/r/c/s/q>` | `q` (quit) |

Check that it works:

```bash
docker run --rm -v "$PWD/rclone:/config/rclone" rclone/rclone lsd dropbox:
```

This should list the folders in your Dropbox.

### 7c. Turn on the backup

```bash
nano .env
```

Remove the `#` in front of `COMPOSE_PROFILES=dropbox`. If you like, change
`DROPBOX_FOLDER` (default `Owncast VODs`) and `DROPBOX_RETENTION_DAYS`. Then:

```bash
docker compose up -d
docker compose logs -f dropbox
```

You should see `dropbox backup: /vods -> dropbox:Owncast VODs every 15 min`,
then upload lines once a VOD has finished. The first upload happens after the
current 4-hour block ends.

| Dropbox setting (`.env`) | Default | What it does |
|---|---|---|
| `COMPOSE_PROFILES` | off | `dropbox` turns the backup on |
| `DROPBOX_FOLDER` | `Owncast VODs` | Folder inside your Dropbox |
| `DROPBOX_INTERVAL_MIN` | `15` | How often to check for finished VODs |
| `DROPBOX_RETENTION_DAYS` | `0` | Delete from Dropbox after this many days (`0` = never) |

---

## 8. Daily use

| Task | Command (in `/opt/owncast-vod`) |
|---|---|
| See what it's doing | `docker compose logs -f --tail 50` |
| Restart | `docker compose restart` |
| Stop | `docker compose down` |
| Change settings | edit `.env`, then `docker compose up -d` |
| Update to the latest version | `git pull && docker compose up -d --build` |
| Check disk usage | `du -sh vods && df -h .` |
| Dropbox backup log | `docker compose logs -f dropbox` |
| JSON list of VODs | `curl -s http://127.0.0.1:8090/vods/api/list` |

### Settings (in `.env`)

| Setting | Default | What it does |
|---|---|---|
| `OWNCAST_URL` | `http://127.0.0.1:8080` | Where Owncast answers on the server |
| `HLS_PATH` | `/hls/stream.m3u8` | Set `/hls/0/stream.m3u8` to record one specific quality instead of the best |
| `SEGMENT_SECONDS` | `14400` | VOD length in seconds (4 h) |
| `TZ` | `America/New_York` | Time zone for the split times |
| `RETENTION_DAYS` | `14` | Delete VODs older than this (`0` = never) |
| `MIN_FREE_GB` | `5` | If free disk drops below this, delete the oldest VODs |
| `TITLE` | `Stream VODs` | Heading on the /vods page |
| `WEBHOOK_TOKEN` | – | Secret required on the webhook URL |
| `PORT` | `8090` | Where the page listens (127.0.0.1 only, reachable through nginx) |
| `VOD_PATH` | `./vods` | Folder on the VPS where VODs are saved |

---

## 9. Troubleshooting

**`/vods` shows 502 Bad Gateway**
The container isn't running. Run `docker compose ps` and `docker compose logs`
in `/opt/owncast-vod`, then `docker compose up -d`.

**`/vods` shows Owncast's page or a 404 from Owncast**
The Vhost block isn't being used. Make sure you pasted it inside the
`listen 443` server block, above `location / {`, and clicked Save.

**Page loads but says "No VODs yet" while you're live**
Run `docker compose logs`:
- `status check failed: ... Connection refused` → `OWNCAST_URL` in `.env` has the wrong
  port. Go back to step 2.
- `recording ...` followed by ffmpeg errors → test the stream URL:
  `curl -s http://127.0.0.1:8080/hls/stream.m3u8` should print a playlist.

**Owncast has no host port** (step 2 showed no `->8080`)
Owncast is only on a Docker network. Either add `ports: ["127.0.0.1:8080:8080"]`
to Owncast's compose file and recreate it, or put the recorder on Owncast's
network: in `docker-compose.yml` remove `network_mode: host` and `BIND`, then add:
```yaml
    ports: ["127.0.0.1:8090:8090"]
    networks: [owncast_default]   # name from: docker network ls
networks:
  owncast_default:
    external: true
```
Then set `OWNCAST_URL=http://<owncast-container-name>:8080` in `.env`.

**Videos won't seek / skip ahead in the browser**
Make sure `proxy_buffering off;` is in the Vhost block. If you use
Cloudflare, large MP4s can hit its limits. Download the file instead, or add
a Cloudflare cache rule that bypasses `/vods/*`.

**Disk filling up**
Lower `RETENTION_DAYS` or raise `MIN_FREE_GB` in `.env`, then run `docker compose up -d`.

**Dropbox log says "remote 'dropbox' is not set up yet"**
Redo step 7b. The remote name must be exactly `dropbox`, and you must run the
command from `/opt/owncast-vod`.

**Dropbox uploads stopped with an auth/token error**
Reconnect: run `rclone authorize "dropbox"` on your computer again, then on
the VPS:
`docker run --rm -it -v "$PWD/rclone:/config/rclone" rclone/rclone config reconnect dropbox:`
and paste the new token. Then run `docker compose restart dropbox`.

**Webhook does nothing**
Check that the token in the URL matches `WEBHOOK_TOKEN` in `.env` exactly. The URL must
start with `https://live.kennethyork.com/vods/webhook`.

---

## 10. Uninstall

```bash
cd /opt/owncast-vod && docker compose down
# remove the /vods block from the CloudPanel Vhost, and the External Action
# and webhook in Owncast admin
rm -rf /opt/owncast-vod        # WARNING: also deletes all saved VODs
```
