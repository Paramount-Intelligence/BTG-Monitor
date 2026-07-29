# BTG Project Monitor — Railway Deployment Guide

Production guide for running the Selenium BTG Project Monitor as a **single-replica** Railway background worker.

> A successful Railway deploy does **not** guarantee BTG will accept the outbound IP. Always validate with `--test-btg-preflight` before relying on login.

---

## Repository files

| File | Purpose |
|------|---------|
| `Dockerfile` | Python 3.12 slim + Debian Chromium/ChromeDriver image |
| `requirements.txt` | Python dependencies (`selenium`, `pymongo`, `requests`, …) |
| `start.sh` | Startup banner + `exec python -u btg_script.py` (SIGTERM reaches Python) |
| `railway.toml` | Dockerfile build, `/health` healthcheck, restart policy |
| `.env.example` | Safe variable template (no secrets) |
| `.dockerignore` | Keeps secrets and junk out of the image |
| `btg_script.py` | Full monitor (scrape, MongoDB, cookies, emails, preflight, health) |

**Use exactly one Railway replica.** Multiple replicas can scan the same projects and send duplicate emails. A MongoDB worker lease also reduces duplicate scanning if more than one process starts.

---

## Railway deployment

1. Push the project to a **private** GitHub repository.
2. Create a Railway project.
3. Add a service from that GitHub repository.
4. Confirm Railway detects the `Dockerfile` (`railway.toml` sets `builder = "DOCKERFILE"`).
5. Add required variables (see checklist below). Copy names from `.env.example`.
6. Set **replicas = 1**.
7. Deploy.
8. Review build and runtime logs (Chromium/ChromeDriver versions should print at start).
9. Run a one-off command (or temporary start command):

```bash
python -u btg_script.py --test-btg-preflight
```

10. Start normal monitoring **only after** preflight succeeds (`BTG_PREFLIGHT_OK`).

Optional diagnostics:

```bash
python -u btg_script.py --print-runtime-diagnostics
python -u btg_script.py --test-btg-login
python -u btg_script.py --test-error-email
```

---

## Healthcheck

`GET /health` on `0.0.0.0:$PORT` (Railway sets `PORT`).

Returns HTTP **200** JSON while the process is alive, including when BTG is temporarily unavailable (`monitor_state: degraded`). External BTG failures must not fail Railway’s healthcheck.

Example fields:

- `status`, `service`, `process_alive`
- `monitor_state` (`starting`, `preflight_check`, `logging_in`, `authenticated`, `scanning`, `sleeping`, `degraded`, `shutting_down`)
- `last_successful_scan`, `last_login_result`, `timestamp`

Other paths return **404**. Credentials, cookies, SMTP, and Mongo URIs are never exposed.

---

## Expected successful preflight

```text
HTTP 200 or 204
Access-Control-Allow-Origin: https://talent.businesstalentgroup.com  (or *)
classification: BTG_PREFLIGHT_OK
```

Command exit code: `0`.

---

## Expected blocked preflight

```text
HTTP 403
server: awselb/2.0
classification: BTG_EDGE_403
```

Credentials are **not** sent. Selenium login is **skipped**. The worker stays alive, sets health to `degraded`, sends one operational alert (with cooldown), and retries after `BTG_PREFLIGHT_FAILURE_RETRY_SECONDS`.

Command exit code: `1`.

---

## Railway variables

### Required

```env
BTG_EMAIL=
BTG_PASSWORD=
MONGO_URI=
SMTP_SERVER=
SMTP_PORT=
SENDER_EMAIL=
SENDER_PASSWORD=
RECIPIENT_EMAILS=
error_recipent=
```

Error-recipient aliases also work: `ERROR_RECIPIENTS`, `ERROR_RECIPIENT`, `ERROR_RECIPENT`, `error_recipent`.

### Recommended production

```env
HEADLESS=true
CHECK_INTERVAL=300
LOGIN_RETRY_INTERVAL=1800
ERROR_EMAIL_COOLDOWN_MINUTES=30

BTG_PREFLIGHT_ENABLED=true
BTG_PREFLIGHT_TIMEOUT=30
BTG_PREFLIGHT_FAILURE_RETRY_SECONDS=1800
BTG_PREFLIGHT_URL=https://api.businesstalentgroup.com/auth/sign_in

CHROME_BIN=/usr/bin/chromium
CHROMEDRIVER_PATH=/usr/bin/chromedriver

TZ=Asia/Karachi
EVIDENCE_DIR=/tmp/btg-evidence
EVIDENCE_RETENTION_HOURS=24
COOKIE_FILE=/tmp/btg_cookies.json

BTG_WORKER_LOCK_ENABLED=true
BTG_WORKER_LOCK_TTL_SECONDS=180
```

`PORT` is provided by Railway — do not require it manually.

MongoDB is the primary store for projects and session cookies. Local files under `/tmp` (or a Railway Volume via `RAILWAY_VOLUME_MOUNT_PATH`) are only for temporary evidence / optional cookie cache.

---

## Static outbound IP

1. Deploy and run `python -u btg_script.py --test-btg-preflight` from Railway.
2. If you enable a Railway **static outbound IP**, record that address in your deployment notes.
3. If BTG supports allowlisting, provide that IP to BTG.
4. Changing Railway **region** may change the outbound IP.
5. Do **not** assume the IP is dedicated forever, and do **not** add proxies or IP rotation in this app.

Moving to Railway does not by itself fix Contabo-style `OPTIONS …/auth/sign_in` → HTTP 403 (`awselb/2.0`) blocks.

---

## Troubleshooting

| Symptom | What to check |
|---------|----------------|
| Chromium binary missing | Image build logs; `CHROME_BIN=/usr/bin/chromium` |
| ChromeDriver mismatch | Debian `chromium` + `chromium-driver` versions in build; avoid manual driver downloads |
| MongoDB connection blocked | Atlas network access / URI; worker stays degraded if lock/store fails open |
| SMTP connection blocked | `SMTP_SERVER`/`PORT`; app passwords; error recipients only |
| BTG preflight 403 | Edge/WAF; outbound IP; contact BTG for allowlist — **no bypass in app** |
| BTG preflight 429 | Rate limit; increase `BTG_PREFLIGHT_FAILURE_RETRY_SECONDS` |
| Restart loop | Config missing should **not** exit — process sleeps degraded; check crash logs |
| Healthcheck failure | `/health` must listen on `PORT`; start health server before login |
| Duplicate worker lock | Keep replicas=1; standby replica sleeps without scanning/emailing |
| Evidence growth | `EVIDENCE_RETENTION_HOURS`; cleanup after cycles |

---

## Security posture (do not change)

This deployment does **not**:

- Transform or rewrite passwords
- Disable web security / site isolation
- Spoof `navigator.webdriver` or custom User-Agents for login
- Add stealth packages, proxies, or CAPTCHA/MFA bypasses

Preflight is a credential-free `OPTIONS` connectivity check only.
