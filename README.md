# BTG Project Monitor

Monitors [BTG (Business Talent Group)](https://talent.businesstalentgroup.com/projects) for new project postings and sends email alerts within 1-2 minutes of a new post appearing.

## How It Works

- Selenium headless Chrome automation
- 60-second polling loop (configurable)
- Age filter — ignores jobs posted more than 60 minutes ago
- De-duplication via MongoDB Atlas (`btg_projects` collection)
- HTML email alerts with title, location, budget, duration, detected time (PKT)
- Self-healing: if a check crashes, the driver restarts automatically
- Never exits: outer restart loop catches all fatal errors and restarts after 30s

---

## Environment Variables

| Variable           | Description                                      |
| ------------------ | ------------------------------------------------ |
| `BTG_EMAIL`        | BTG login email                                  |
| `BTG_PASSWORD`     | BTG login password                               |
| `SMTP_SERVER`      | SMTP server (default: smtp.gmail.com)            |
| `SMTP_PORT`        | SMTP port (default: 587)                         |
| `SENDER_EMAIL`     | Gmail address to send alerts from                |
| `SENDER_PASSWORD`  | Gmail app password                               |
| `RECIPIENT_EMAILS` | Comma-separated list of alert recipients         |
| `CHECK_INTERVAL`   | Seconds between checks (default: 60)             |
| `MAX_AGE_MINUTES`  | Ignore jobs older than this (default: 60)        |
| `MONGO_URI`        | MongoDB Atlas connection string                  |
| `HEADLESS`         | Set to `True` on server (default: False locally) |

---

## Local Development

```bash
pip install -r requirements.txt
python btg_script.py          # runs continuously
python btg_script.py --once   # one check then exit (for testing)
python btg_script.py --debug  # prints page structure for selector debugging
```

---

## Railway Deployment

1. Push this folder to a GitHub repo
2. Create a new Railway service → connect the repo
3. Railway auto-detects `railway.toml` and builds via `Dockerfile`
4. Add all environment variables in Railway → Variables tab
5. Set `HEADLESS=True`
6. Deploy — service runs forever with `restartPolicyType = ALWAYS`

---

## Normal Run

```bash
python btg_script.py
```

Runs forever, checks every 60 seconds.  
Stop with `Ctrl+C`.

---

## Files

| File               | Purpose                                  |
| ------------------ | ---------------------------------------- |
| `btg_script.py`    | Main monitoring loop                     |
| `.env`             | Credentials & config                     |
| `btg_cookies.json` | Session cookie cache (auto-created)      |
| `btg_projects.db`  | De-duplication DB (SQLite, auto-created) |
