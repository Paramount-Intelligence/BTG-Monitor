# BTG Project Monitor

Monitors [BTG (Business Talent Group)](https://talent.businesstalentgroup.com/projects) for new project postings and sends email alerts within 1-2 minutes of a new post appearing.

- Uses Selenium headless Chrome to scrape the BTG projects page every 60 seconds
- Detects new postings by comparing against MongoDB Atlas (de-duplication by project ID)
- On every startup, reconciles all currently visible jobs as already seen so restarts never re-send old alerts
- Sessions are persisted in MongoDB (cookies survive container restarts); re-authenticates automatically if the session expires mid-run
- Sends a rich HTML email to all configured recipients with the project title, description, location, budget, timeline, requirements, and a direct link
- Self-healing: inner exceptions restart the Chrome driver; outer loop catches fatal crashes and restarts after 30 seconds
