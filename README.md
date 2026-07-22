# BTG Project Monitor

Monitors [BTG (Business Talent Group)](https://talent.businesstalentgroup.com/projects) for new project postings and sends email alerts within 1-2 minutes of a new post appearing.

- Uses Selenium headless Chrome to scrape the BTG projects page every 60 seconds
- Detects new postings by comparing against MongoDB Atlas (de-duplication by project ID + posted date so re-posts are treated as new)
- On every startup, reconciles all currently visible jobs as already seen so restarts never re-send old alerts
- Sessions are persisted in MongoDB (cookies survive container restarts); expired cookies are cleared and the monitor re-authenticates automatically
- Sends a rich HTML email to all configured recipients with the project title, description, location, budget, timeline, requirements, and a direct link
- Sends operational error alerts (login failures, CAPTCHA/MFA, browser/Mongo issues, etc.) only to `error_recipent`
- Self-healing: authentication failures wait `LOGIN_RETRY_INTERVAL` (default 300s) once before retrying — no double 60s+30s restart loop

## Environment variables

```env
BTG_EMAIL=your-btg-email@example.com
BTG_PASSWORD=your-btg-password

SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SENDER_EMAIL=your-sender@example.com
SENDER_PASSWORD=your-app-password

RECIPIENT_EMAILS=project1@example.com,project2@example.com

error_recipent=operations@example.com
ERROR_EMAIL_COOLDOWN_MINUTES=30
LOGIN_RETRY_INTERVAL=300

CHECK_INTERVAL=60
HEADLESS=true
MONGO_URI=mongodb://localhost:27017/
```

The misspelled key `error_recipent` is intentional and supported. Aliases `ERROR_RECIPENT`, `ERROR_RECIPIENT`, and `ERROR_RECIPIENTS` also work.

## Useful commands

```bash
python btg_script.py
python btg_script.py --once
python btg_script.py --test
python btg_script.py --test-error-email
python btg_script.py --debug
```
