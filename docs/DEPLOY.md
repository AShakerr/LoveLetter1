# Deploying desk on the Hetzner box

One container, one SQLite file, Caddy in front. This runs next to the IMPROVR dashboard on the same host:
Caddy on the host terminates TLS for both, each app listens on its own localhost port, and the existing
backup cron gains one more rsync line. Every command below is run by hand as your normal SSH user; nothing
here is scripted or automatic except the schedules that the container and cron own.

Placeholders to replace: `desk.example.com` (your hostname), `BACKUP_TARGET` (the rsync destination the
IMPROVR backup already uses), `ahmed` (basic-auth user).

## 0. What runs where

| Piece | Where | Owner |
|---|---|---|
| FastAPI + APScheduler (`desk`) | container `desk-app-1`, port 127.0.0.1:8000 | Docker Compose, `restart: unless-stopped` |
| TLS + reverse proxy | Caddy on the host, `/etc/caddy/Caddyfile` | systemd `caddy` |
| Basic auth | inside the app, every route, from `.env` | the app |
| SQLite, inbox, archive, cache, backups | `/srv/desk/data` (bind-mounted as `/data`) | you |
| Daily fetch + decisions 07:00 Europe/Berlin, backup 02:00, inbox scan every 5 min, fundamentals Sunday 06:00, constituents 1st of month 05:30, digest Monday 07:30 | APScheduler inside the container, timezone from `TZ` | the app |
| rsync of `/srv/desk/data/backups` to the backup target, 02:30 | host crontab | cron |

The scheduler lives in the web process, so "the server is up" and "the jobs run" are the same thing.
If the container is down at 07:00 the day's run is skipped, not queued; run `desk decide` by hand when
it is back (section 8).

## 1. Prerequisites on the host

Docker Engine with the Compose plugin and Caddy are already there for IMPROVR. Check:

```
docker compose version
caddy version
systemctl is-active caddy
```

Open the DNS record first: an `A` (and `AAAA` if the box has IPv6) record for `desk.example.com` pointing at
the host. Caddy needs it resolvable before it can obtain the certificate.

## 2. Check out the code

```
sudo mkdir -p /srv/desk /srv/desk/data
sudo chown -R "$USER":"$USER" /srv/desk
cd /srv/desk
git clone https://github.com/AShakerr/LoveLetter1.git app
cd app
git checkout claude/new-session-6a8ys4
```

Later updates are `cd /srv/desk/app && git pull && docker compose up -d --build`.

## 3. Data directory as a bind mount

`docker-compose.yml` uses a named volume. On this host a bind mount is easier to back up and to drop files
into, so add an override file (Compose picks it up automatically; keep it out of git):

```
cat > /srv/desk/app/docker-compose.override.yml <<'EOF'
services:
  app:
    volumes:
      - /srv/desk/data:/data
EOF
```

`/srv/desk/data` will hold `desk.sqlite3`, `backups/`, `cache/`, `inbox/`, `archive/`, `digests/` and the
kill-switch file `KILL`. The container runs as root, so files it creates are root-owned; that is fine for a
single-user box. If you prefer your own user, add `user: "1000:1000"` to the override and
`sudo chown -R 1000:1000 /srv/desk/data` once.

## 4. `.env`

```
cd /srv/desk/app
cp .env.example .env
chmod 600 .env
nano .env
```

Fill in:

```
ANTHROPIC_API_KEY=...
FRED_API_KEY=...
ALPHAVANTAGE_API_KEY=...
DESK_BASIC_AUTH_USER=ahmed
DESK_BASIC_AUTH_PASS=<long random password>
TZ=Europe/Berlin
DESK_BROKER=paper
```

Generate the password with `openssl rand -base64 24`. Leave `DESK_LIVE` unset: the live adapters are stubs
and the guard refuses to start execution without it anyway. Leave `DESK_ALLOW_NO_AUTH` unset on the server;
it exists for local development only. `DESK_DATA_DIR` in `.env` is ignored inside the container (the image
sets `/data`), so no change there.

Optional: `DESK_DIGEST_TO` plus the `DESK_SMTP_*` lines for the Monday e-mail; without them the digest is
written to `/srv/desk/data/digests/`. `DESK_LLM_REASONING=1` adds a Claude-written paragraph to every
decision (costs one call per decision per day).

Never commit `.env`. It is listed in `.gitignore`; check with `git status` after editing.

## 5. Build and start

```
cd /srv/desk/app
docker compose up -d --build
docker compose logs -f app
```

You should see the uvicorn "Application startup complete" line within a few seconds. Ctrl+C leaves the
container running. Then:

```
curl -s http://127.0.0.1:8000/healthz
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/
```

The first prints `{"ok":true,"version":"..."}`; the second prints `401`, which proves basic auth is on.

## 6. Seed the database

The container starts with an empty database. Load your positions, house views, kill conditions and regime
seed, then confirm the position batch:

```
docker compose exec app uv run desk init-db
docker compose exec app uv run desk seed
docker compose exec app uv run desk positions
docker compose exec app uv run desk positions --confirm seed:positions_2026-09-04
```

Then the first live pull, in this order (constituents need the network, fundamentals take several minutes):

```
docker compose exec app uv run desk fetch
docker compose exec app uv run desk screener --refresh
docker compose exec app uv run desk fundamentals
docker compose exec app uv run desk screener
docker compose exec app uv run desk decide
```

Alpha Vantage allows 25 calls a day; `desk fetch` uses up to five. Do not run it more than a couple of times
on the first day.

If you want the exact database state from the Mac instead, copy it: stop the container, `scp` the Mac's
`data/desk.sqlite3` to `/srv/desk/data/desk.sqlite3`, start again. Do not copy while either side is running.

## 7. Caddy

Append a site block to the existing Caddyfile, next to the IMPROVR one:

```
sudo nano /etc/caddy/Caddyfile
```

```
desk.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

```
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
curl -s -o /dev/null -w "%{http_code}\n" https://desk.example.com/
```

Expect `401`. Open the URL in a browser, enter the basic-auth user and password from `.env`, and the tape
should show today's numbers with their sources. Caddy obtains and renews the Let's Encrypt certificate
itself. The app also serves `/healthz` without auth, which Caddy's proxy passes through; nothing else is
reachable without the password.

If the IMPROVR dashboard uses Caddy's own `basicauth` directive, do not add it here: the app already
enforces its own, and two prompts would be confusing.

## 8. The daily job

At 07:00 Europe/Berlin the container runs, in one job: fetch every source, settle yesterday's paper orders,
recompute the regime, score the universe, run the rules, write the day's decisions, submit mandatory exits to
the paper broker, run the screener. The schedule is fixed by `TZ` and the `DESK_DAILY_HOUR` /
`DESK_DAILY_MINUTE` settings; 07:00 is the default.

Check it ran:

```
docker compose logs --since 24h app | grep -E "decisions|fetch|error" | tail -20
```

Or open the Tape page: the Sources table shows the last run time and row count per source. A red status
there is what the daily run could not fetch; the last-good cache in `/data/cache` keeps the previous payload
in use so a single failed source never blanks the dashboard.

Run the job by hand after downtime:

```
docker compose exec app uv run desk fetch
docker compose exec app uv run desk decide
```

Both are idempotent for the day: rerunning creates no duplicate decisions or orders.

Kill switch: `touch /srv/desk/data/KILL` stops all paper execution until you release it from the Decisions
page (type CONFIRM). Restarting the container does not clear it.

## 9. Backups

Inside the container, 02:00 Europe/Berlin: `sqlite3` online backup to `/data/backups/desk_<stamp>.sqlite3`,
newest 30 kept. That is a consistent copy even while the app is writing. On the host that directory is
`/srv/desk/data/backups`.

Ship it off the box with the IMPROVR backup job. Add one line to the same crontab, 30 minutes after the
container has written its file:

```
crontab -e
```

```
30 2 * * * rsync -a --delete /srv/desk/data/backups/ BACKUP_TARGET/desk-backups/ >> /var/log/desk-backup.log 2>&1
```

`BACKUP_TARGET` is whatever the IMPROVR line already uses (`user@host:/path` or a mounted storage box path).
`--delete` mirrors the 30-file rotation to the target; drop it if you want the target to keep everything.
Use the same SSH key the IMPROVR rsync uses; nothing new to authorise.

Also back up `.env` once, by hand, somewhere private (password manager). It is not in the data directory
and is not in git.

Test a restore before you need it:

```
sqlite3 /srv/desk/data/backups/desk_<stamp>.sqlite3 "select count(*) from decisions; select max(date) from observations;"
```

To restore: stop the container, copy the backup over `/srv/desk/data/desk.sqlite3`, start the container.

## 10. Routine operations

| Task | Command |
|---|---|
| Update code | `cd /srv/desk/app && git pull && docker compose up -d --build` |
| Logs | `docker compose logs -f app` |
| Shell in the container | `docker compose exec app bash` |
| Any desk command | `docker compose exec app uv run desk <command>` |
| Drop a Safra PDF or Revolut screenshot | copy into `/srv/desk/data/inbox/` (or `inbox/portfolio/` for screenshots); the 5-minute scan picks it up |
| Weekly digest by hand | `docker compose exec app uv run desk digest` |
| Track record | `docker compose exec app uv run desk track-record` |
| Manual backup now | `docker compose exec app uv run desk backup` |
| Stop / start | `docker compose stop` / `docker compose start` |

## 11. What to look at after the first unattended week

- Tape page, Sources table: every source green at 07:0x each day. GDELT partial results are normal; a red
  ECB or FRED is not.
- Decisions page: one row per held instrument per day, no duplicates, mandatory rules only where a limit was
  really breached.
- Paper vs actual on the Decisions page: paper and actual quantities equal unless you approved a BUY.
- `/srv/desk/data/backups`: seven new files, the rsync log without errors.
- Track record page: outcomes start to fill from the 30th day.
