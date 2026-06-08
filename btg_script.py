import time
import smtplib
import json
import os
import re
import sys
from pymongo import MongoClient, UpdateOne
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver.chrome.options import Options
from dotenv import load_dotenv

# Load .env file from this script's directory
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

PKT = timezone(timedelta(hours=5))  # Pakistan Standard Time (UTC+5)

# ============================
# CONFIGURATION
# ============================
class Config:
    BTG_EMAIL    = os.getenv("BTG_EMAIL")
    BTG_PASSWORD = os.getenv("BTG_PASSWORD")
    SMTP_SERVER  = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    SMTP_PORT    = int(os.getenv("SMTP_PORT", 587))
    SENDER_EMAIL    = os.getenv("SENDER_EMAIL")
    SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
    RECIPIENT_EMAILS = [
        e.strip() for e in
        os.getenv("RECIPIENT_EMAILS",
                  "ahmedghazi495@gmail.com,ahsanuddin3522@gmail.com")
        .split(",") if e.strip()
    ]
    CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL", 60))
    MAX_AGE_MINUTES = int(os.getenv("MAX_AGE_MINUTES", 60))
    HEADLESS     = os.getenv("HEADLESS", "False").lower() == "true"
    COOKIES_FILE = os.getenv("BTG_COOKIES_FILE", "btg_cookies.json")
    MONGO_URI    = os.getenv("MONGO_URI", "mongodb://localhost:27017/")

    # BTG URLs
    BASE_URL     = "https://talent.businesstalentgroup.com"
    LOGIN_URL    = "https://talent.businesstalentgroup.com/login"
    PROJECTS_URL = "https://talent.businesstalentgroup.com/projects"

# ============================
# DEBUG HELPERS
# ============================
DEBUG_MODE = "--debug" in sys.argv
ONCE_MODE  = "--once"  in sys.argv  # Run one check then exit (for testing)
TEST_MODE  = "--test"  in sys.argv  # Skip MongoDB, send 1 test email only

def debug_print(msg):
    if DEBUG_MODE:
        print(msg)

def dump_page_structure(driver):
    """Print a summary of page elements when DEBUG mode is on or selectors fail."""
    print("\n" + "="*60)
    print("🔍 PAGE STRUCTURE DUMP (to identify correct selectors)")
    print("="*60)
    print(f"  URL: {driver.current_url}")

    # Look for any card-like containers
    card_candidates = [
        "article", ".card", "[class*='card']", "[class*='project']",
        "[class*='opportunity']", "[class*='job']", "[class*='listing']",
        "li[class]", "div[class*='item']"
    ]
    print("\n📦 Card-like containers found:")
    for sel in card_candidates:
        try:
            elems = driver.find_elements(By.CSS_SELECTOR, sel)
            if elems:
                sample = elems[0]
                cls = sample.get_attribute("class") or ""
                tag = sample.tag_name
                txt_preview = sample.text[:80].replace("\n", " ") if sample.text else "(no text)"
                print(f"  [{len(elems)}] {sel}  → <{tag} class='{cls[:60]}'> text='{txt_preview}'")
        except:
            pass

    # Headings inside divs
    print("\n📝 Headings / Title elements:")
    for sel in ["h1", "h2", "h3", "h4", "[class*='title']", "[class*='name']"]:
        try:
            elems = driver.find_elements(By.CSS_SELECTOR, sel)
            if elems:
                for e in elems[:3]:
                    txt = e.text.strip()[:80] if e.text else ""
                    if txt:
                        print(f"  <{e.tag_name} class='{(e.get_attribute('class') or '')[:50]}'> → {txt}")
        except:
            pass

    print("\n⏰ Time / Posted elements:")
    for sel in ["[class*='time']", "[class*='date']", "[class*='posted']", "[class*='ago']", "time"]:
        try:
            elems = driver.find_elements(By.CSS_SELECTOR, sel)
            if elems:
                for e in elems[:3]:
                    txt = e.text.strip()[:80] if e.text else ""
                    if txt:
                        print(f"  <{e.tag_name} class='{(e.get_attribute('class') or '')[:50]}'> → {txt}")
        except:
            pass

    print("\n💰 Budget / Rate elements:")
    for sel in ["[class*='budget']", "[class*='rate']", "[class*='pay']", "[class*='compensation']", "[class*='salary']"]:
        try:
            elems = driver.find_elements(By.CSS_SELECTOR, sel)
            if elems:
                for e in elems[:3]:
                    txt = e.text.strip()[:80] if e.text else ""
                    if txt:
                        print(f"  <{e.tag_name} class='{(e.get_attribute('class') or '')[:50]}'> → {txt}")
        except:
            pass

    print("="*60 + "\n")


# ============================
# SESSION MANAGEMENT
# ============================
def _get_session_collection():
    """Separate MongoDB collection for storing session cookies."""
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(Config.MONGO_URI)
    return _mongo_client["office_monitor"]["sessions"]

def save_cookies(driver):
    """Save cookies to MongoDB (survives container restarts) AND local file as backup."""
    cookies = driver.get_cookies()
    # MongoDB
    try:
        _get_session_collection().update_one(
            {"_id": "btg_cookies"},
            {"$set": {"cookies": cookies, "saved_at": datetime.now(timezone.utc)}},
            upsert=True
        )
    except Exception as e:
        print(f"  ⚠️ Could not save cookies to MongoDB: {e}")
    # Local file fallback
    try:
        path = os.path.join(os.path.dirname(__file__), Config.COOKIES_FILE)
        with open(path, 'w') as f:
            json.dump(cookies, f)
    except Exception:
        pass
    return True

def load_cookies(driver):
    """Load cookies from MongoDB first, fall back to local file."""
    cookies = None
    # Try MongoDB first
    try:
        doc = _get_session_collection().find_one({"_id": "btg_cookies"})
        if doc and doc.get("cookies"):
            cookies = doc["cookies"]
            print("  Loaded cookies from MongoDB")
    except Exception as e:
        print(f"  ⚠️ Could not load cookies from MongoDB: {e}")
    # Fall back to local file
    if not cookies:
        path = os.path.join(os.path.dirname(__file__), Config.COOKIES_FILE)
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    cookies = json.load(f)
                print("  Loaded cookies from local file")
            except Exception:
                pass
    if not cookies:
        return False
    try:
        driver.get(Config.BASE_URL)
        time.sleep(2)
        driver.delete_all_cookies()
        for cookie in cookies:
            try:
                driver.add_cookie(cookie)
            except Exception:
                pass
        return True
    except Exception:
        return False

def perform_login(driver):
    """Log in to BTG."""
    try:
        print(f"  Navigating to: {Config.LOGIN_URL}")
        driver.get(Config.LOGIN_URL)
        time.sleep(3)

        # --- dismiss OneTrust / cookie consent overlay FIRST ---
        for consent_sel in [
            "#onetrust-accept-btn-handler",
            "button.onetrust-accept-btn-handler",
            "button#accept-recommended-btn-handler",
            "button[aria-label*='Accept']",
            "button[title*='Accept All']",
            ".onetrust-close-btn-handler",
        ]:
            try:
                btn = driver.find_element(By.CSS_SELECTOR, consent_sel)
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(2)
                print("  Cookie consent dismissed")
                break
            except NoSuchElementException:
                pass

        # Scroll to top to make sure form is visible
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(1)

        # --- email field ---
        email_field = None
        for sel in ['input[type="email"]', 'input[name="email"]', 'input[id*="email"]',
                    'input[placeholder*="email" i]']:
            try:
                email_field = WebDriverWait(driver, 8).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                )
                break
            except TimeoutException:
                continue

        if not email_field:
            print("❌ Could not find email field.")
            dump_page_structure(driver)
            return False

        # Click field first (important for Angular reactive forms)
        email_field.click()
        time.sleep(0.3)
        email_field.clear()
        email_field.send_keys(Config.BTG_EMAIL)
        time.sleep(0.5)

        # --- password field ---
        password_field = None
        for sel in ['input[type="password"]', 'input[name="password"]', 'input[id*="password"]']:
            try:
                password_field = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                )
                break
            except (TimeoutException, NoSuchElementException):
                continue

        if not password_field:
            print("❌ Could not find password field.")
            return False

        password_field.click()
        time.sleep(0.3)
        password_field.clear()
        password_field.send_keys(Config.BTG_PASSWORD)
        time.sleep(0.5)

        # --- submit: try pressing Enter first (most reliable for Angular), then button click ---
        from selenium.webdriver.common.keys import Keys
        try:
            password_field.send_keys(Keys.RETURN)
            print("  Submitted via Enter key")
        except Exception:
            # Fall back to finding and clicking submit button
            submit_btn = None
            for sel in ['button[type="submit"]', 'input[type="submit"]']:
                try:
                    submit_btn = driver.find_element(By.CSS_SELECTOR, sel)
                    break
                except NoSuchElementException:
                    continue
            if not submit_btn:
                try:
                    submit_btn = driver.find_element(
                        By.XPATH,
                        "//button[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                        "'abcdefghijklmnopqrstuvwxyz'),'sign') or @type='submit']"
                    )
                except NoSuchElementException:
                    print("❌ Could not find submit button.")
                    return False
            driver.execute_script("arguments[0].click();", submit_btn)
            print("  Submitted via JS button click")

        # Wait for redirect (up to 15s)
        for _ in range(15):
            time.sleep(1)
            if "login" not in driver.current_url.lower() and "sign-in" not in driver.current_url.lower():
                break
        else:
            print(f"❌ Still on login page after submit. URL: {driver.current_url}")
            print("   Possible causes: wrong password, CAPTCHA, or 2-step auth")
            return False

        save_cookies(driver)
        print(f"✅ Login successful → {driver.current_url}")
        return True

    except Exception as e:
        print(f"❌ Login error: {e}")
        return False


# ============================
# PROJECT EXTRACTION
# ============================

# ── SELECTOR SETS ────────────────────────────────────────────────────────────
# Updated based on live BTG page structure dump.
CARD_SELECTORS = [
    "article",
    ".project-card",
    "[class*='project-card']",
    "[class*='project-item']",
]

# BTG confirmed: titles are in <h4> and <div class='name'>
TITLE_SELECTORS = [
    "h4",
    "div.name",
    "h3", "h2",
    "[class*='title']",
    "[class*='heading']",
]

# BTG confirmed: <div class='detail date-posted'>Posted:\n03/04/2026</div>
TIME_SELECTORS = [
    "div.detail.date-posted",
    ".detail.date-posted",
    "[class*='date-posted']",
    "[class*='posted']",
    "time",
]

# BTG confirmed: <div class='detail location'>
LOCATION_SELECTORS = [
    "div.detail.location",
    ".detail.location",
    "[class*='location']",
    "[class*='remote']",
    "address",
]

# BTG confirmed: <div class='detail budget ng-star-inserted'>
BUDGET_SELECTORS = [
    "div.detail.budget",
    ".detail.budget",
    "[class*='budget']",
    "[class*='rate']",
    "[class*='compensation']",
]

# BTG shows "Starts:" date — no explicit duration; keep as fallback
DURATION_SELECTORS = [
    "div.detail.start-date",
    ".detail.start-date",
    "[class*='duration']",
    "[class*='timeline']",
    "[class*='start']",
]

CATEGORY_SELECTORS = [
    "[class*='category']",
    "[class*='tag']",
    "[class*='skill']",
    "[class*='practice']",
    "[class*='label']",
]

DESCRIPTION_SELECTORS = [
    "[class*='description']",
    "[class*='summary']",
    "[class*='overview']",
    "p",
]


def _first_text(parent, selectors, max_len=200, skip_labels=None):
    """Return text from first matching child element, or empty string.
    skip_labels: list of label strings to strip from the start of extracted text.
    """
    if skip_labels is None:
        skip_labels = ["Posted:", "Location:", "Budget:", "Starts:", "Duration:"]
    for sel in selectors:
        try:
            elems = parent.find_elements(By.CSS_SELECTOR, sel)
            for e in elems:
                t = e.text.strip()
                if t:
                    # Strip label prefixes like "Posted:\n"
                    for label in skip_labels:
                        if t.lower().startswith(label.lower()):
                            t = t[len(label):].strip()
                    # Remove Material icon text (single lowercase words: 'savings', 'place', etc.)
                    ICON_NAMES = {"savings", "place", "insert_invitation", "schedule",
                                  "location_on", "attach_money", "event", "timer",
                                  "work", "business", "person", "star", "info"}
                    lines = [l.strip() for l in t.splitlines()
                             if l.strip() and l.strip().lower() not in ICON_NAMES]
                    t = " ".join(lines)
                    if t:
                        return t[:max_len]
        except Exception:
            pass
    return ""


def extract_project_id(card):
    """Try multiple patterns to extract a unique project ID from a card."""
    # 1. BTG confirmed: look for "View Project" link or any project-detail href
    try:
        links = card.find_elements(By.TAG_NAME, "a")
        for a in links:
            href = a.get_attribute("href") or ""
            # e.g. /projects/12345 or /projects/interim-coo-abc123
            m = re.search(r'/projects?/([a-zA-Z0-9_-]+)', href)
            if m:
                return m.group(1)
            # Catch other path patterns
            m = re.search(r'(?:opportunit[yi]|job|need)[s]?/([a-zA-Z0-9_-]+)', href)
            if m:
                return m.group(1)
    except Exception:
        pass

    # 2. data-* attributes
    try:
        for attr in ("data-id", "data-project-id", "data-opportunity-id", "id"):
            val = card.get_attribute(attr)
            if val and re.match(r'^[a-zA-Z0-9_-]{4,}$', val):
                return val
    except Exception:
        pass

    # 3. Fallback: hash of title + time
    title = _first_text(card, TITLE_SELECTORS, 100)
    if title:
        import hashlib
        return hashlib.md5(title.encode()).hexdigest()[:12]

    return None


def extract_project_data(card):
    """Extract all relevant fields from a BTG project card."""
    try:
        title = _first_text(card, TITLE_SELECTORS, 150)
        # Strip trailing "View Project" that BTG appends to div.name text
        title = re.sub(r'\s*View Project\s*$', '', title, flags=re.IGNORECASE).strip()
        if not title:
            return None

        project_id = extract_project_id(card)
        if not project_id:
            return None

        time_posted = _first_text(card, TIME_SELECTORS, 60) or "Unknown"
        # Clean up: remove "Posted" prefix if present
        time_posted = re.sub(r'(?i)^posted\s*', '', time_posted).strip()

        location   = _first_text(card, LOCATION_SELECTORS, 80)
        budget     = _first_text(card, BUDGET_SELECTORS, 80)
        duration   = _first_text(card, DURATION_SELECTORS, 80)

        # Dollar amount fallback for budget
        if not budget:
            try:
                for el in card.find_elements(By.XPATH, ".//*[contains(text(),'$')]"):
                    t = el.text.strip()
                    if '$' in t and len(t) < 60:
                        budget = t
                        break
            except Exception:
                pass

        # Week/month keyword fallback for duration
        if not duration:
            try:
                for el in card.find_elements(By.XPATH, ".//*[text()]"):
                    t = el.text.strip()
                    if any(w in t.lower() for w in ("week", "month", "day")) and 2 < len(t) < 50:
                        duration = t
                        break
            except Exception:
                pass

        # URL
        url = f"https://talent.businesstalentgroup.com/projects/{project_id}"

        # Status badge
        status = "Posted"
        try:
            card.find_element(By.CSS_SELECTOR, "[class*='new'], .badge-success, [class*='badge']")
            badge_text = card.find_element(By.CSS_SELECTOR, "[class*='new'], .badge-success, [class*='badge']").text.strip().lower()
            if "new" in badge_text:
                status = "New Project"
        except Exception:
            pass

        description = _first_text(card, DESCRIPTION_SELECTORS, 500)

        return {
            "id":          project_id,
            "title":       title,
            "description": description,
            "location":    location,
            "budget":      budget,
            "duration":    duration,
            "time_posted": time_posted,
            "status":      status,
            "url":         url,
            "detected_at": datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception:
        return None


def find_project_cards(driver):
    """Find project card elements using multiple strategies."""

    # ── Strategy 1: from date-posted div, walk up to the ancestor that has an h4
    # This handles cases where date-posted is nested inside the card, not a direct child.
    try:
        date_divs = driver.find_elements(By.CSS_SELECTOR, "div.detail.date-posted, .detail.date-posted")
        # Deduplicate (CSS comma selector may return same element twice)
        seen_ids = set()
        unique_date_divs = []
        for d in date_divs:
            if d.id not in seen_ids:
                seen_ids.add(d.id)
                unique_date_divs.append(d)

        if unique_date_divs:
            cards = []
            card_ids = set()
            for d in unique_date_divs:
                try:
                    # Walk up to the nearest ancestor that contains an h4 or div.name
                    card = d.find_element(By.XPATH,
                        "ancestor::*[.//h4 or .//div[@class='name']][1]"
                    )
                    if card.id not in card_ids:
                        card_ids.add(card.id)
                        cards.append(card)
                except Exception:
                    pass
            if cards:
                debug_print(f"  Using ancestor-of-date-posted strategy ({len(cards)} cards)")
                return cards
    except Exception:
        pass

    # ── Strategy 2: CSS selectors (generic fallback)
    for sel in CARD_SELECTORS:
        try:
            cards = driver.find_elements(By.CSS_SELECTOR, sel)
            if cards:
                debug_print(f"  Using CSS card selector: '{sel}' ({len(cards)} cards)")
                return cards
        except Exception:
            pass

    # ── Strategy 3: XPath — find divs that contain exactly one date-posted AND one h4
    xpath_strategies = [
        "//div[count(.//div[contains(@class,'date-posted')])=1 and .//h4]",
        "//li[count(.//div[contains(@class,'date-posted')])=1 and .//h4]",
    ]
    for xpath in xpath_strategies:
        try:
            cards = driver.find_elements(By.XPATH, xpath)
            if cards:
                debug_print(f"  Using XPath count strategy ({len(cards)} cards)")
                return cards
        except Exception:
            pass

    return []


def scan_for_projects(driver):
    """Navigate to projects page and extract all visible projects."""
    try:
        # Navigate if not already there
        if Config.PROJECTS_URL not in driver.current_url:
            driver.get(Config.PROJECTS_URL)
            time.sleep(4)

        # Wait for page content
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(2)  # extra settle time for JS rendering

        cards = find_project_cards(driver)

        if not cards:
            print("⚠️  No project cards found with default selectors.")
            dump_page_structure(driver)
            return []

        projects = []
        for card in cards:
            p = extract_project_data(card)
            if p and p.get("title") and p.get("id"):
                projects.append(p)

        print(f"✅ Extracted {len(projects)} valid projects from {len(cards)} cards")
        return projects

    except TimeoutException:
        print("⏳ Timeout waiting for BTG projects page")
        return []
    except Exception as e:
        print(f"❌ Error scanning BTG: {e}")
        return []


# ============================
# PROJECT DATABASE (MongoDB)
# ============================
_mongo_client = None

def _get_collection():
    """Return the MongoDB collection, reusing the client across calls."""
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(Config.MONGO_URI)
    return _mongo_client["office_monitor"]["projects"]

def init_db():
    """Ensure a unique index on 'project_id' exists."""
    try:
        _get_collection().create_index("project_id", unique=True, name="idx_project_id_unique")
    except Exception:
        pass  # Index already exists — safe to ignore

def db_is_cold_start():
    """True if the collection has no documents (first ever run)."""
    return _get_collection().find_one({}, {"_id": 1}) is None

def get_seen_ids():
    """Return set of all project IDs already in DB."""
    try:
        docs = _get_collection().find({}, {"project_id": 1, "_id": 0})
        return {d["project_id"] for d in docs if d.get("project_id")}
    except Exception:
        return set()

def insert_project(project, emailed=True):
    """Upsert one project record. Silently skips if ID already exists."""
    try:
        doc = {
            "project_id":       project.get("id"),
            "title":            project.get("title"),
            "description":      project.get("description"),
            "location":         project.get("location"),
            "budget":           project.get("budget"),
            "duration":         project.get("duration"),
            "start_date":       project.get("start_date"),
            "project_length":   project.get("project_length"),
            "level_of_support": project.get("level_of_support"),
            "industry":         project.get("industry"),
            "time_posted":      project.get("time_posted"),
            "status":           project.get("status"),
            "url":              project.get("url"),
            "detected_at":      project.get("detected_at"),
            "platform":         "btg",
            "emailed":          bool(emailed),
        }
        _get_collection().update_one(
            {"project_id": doc["project_id"]},
            {"$setOnInsert": doc},
            upsert=True,
        )
    except Exception as e:
        print(f"⚠️ DB insert failed: {e}")

def bulk_insert_projects(projects, emailed=False):
    """Upsert many projects at once (used for cold-start seeding)."""
    try:
        ops = []
        for p in projects:
            if not p.get("id"):
                continue
            doc = {
                "project_id":  p.get("id"),
                "title":       p.get("title"),
                "location":    p.get("location"),
                "budget":      p.get("budget"),
                "duration":    p.get("duration"),
                "time_posted": p.get("time_posted"),
                "status":      p.get("status"),
                "url":         p.get("url"),
                "detected_at": p.get("detected_at"),
                "platform":    "btg",
                "emailed":     bool(emailed),
            }
            ops.append(UpdateOne({"project_id": doc["project_id"]}, {"$setOnInsert": doc}, upsert=True))
        if ops:
            result = _get_collection().bulk_write(ops, ordered=False)
            print(f"  DB: inserted {result.upserted_count} records (emailed={'yes' if emailed else 'no'})")
    except Exception as e:
        print(f"⚠️ DB bulk insert failed: {e}")


# ============================
# AGE FILTERING
# ============================
def parse_posted_minutes(time_str):
    """Convert 'time_posted' string → minutes elapsed. Returns None if unparseable.
    Handles:
      - Relative: '2 hours ago', '30 minutes ago'
      - Absolute: 'MM/DD/YYYY' or 'YYYY-MM-DD' (BTG format)
    """
    if not time_str or time_str == "Unknown":
        return None
    s = time_str.strip()

    # Relative time (Catalant-style)
    sl = s.lower()
    if any(w in sl for w in ("just", "moment", "second", "now")):
        return 0
    rel = re.search(r'(\d+)\s*(minute|hour|day|week|month)', sl)
    if rel:
        val, unit = int(rel.group(1)), rel.group(2)
        return val * {"minute": 1, "hour": 60, "day": 1440, "week": 10080, "month": 43200}[unit]

    # Absolute date — BTG format MM/DD/YYYY
    m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', s)
    if m:
        try:
            posted_date = datetime(int(m.group(3)), int(m.group(1)), int(m.group(2))).date()
            today = datetime.now().date()
            days_old = (today - posted_date).days
            # If posted today, treat as 0 min (we don't have time granularity)
            # If posted N days ago, treat as N * 1440 min
            return max(days_old * 1440, 0)
        except Exception:
            pass

    # Absolute date — ISO YYYY-MM-DD
    m2 = re.search(r'(\d{4})-(\d{2})-(\d{2})', s)
    if m2:
        try:
            posted_date = datetime(int(m2.group(1)), int(m2.group(2)), int(m2.group(3))).date()
            today = datetime.now().date()
            days_old = (today - posted_date).days
            return max(days_old * 1440, 0)
        except Exception:
            pass

    return None


def filter_new_projects(all_projects, seen_ids):
    """Remove already-seen IDs and jobs older than MAX_AGE_MINUTES."""
    result = []
    for p in all_projects:
        if not p.get("id") or p["id"] in seen_ids:
            continue
        age = parse_posted_minutes(p.get("time_posted", ""))
        if age is not None and age > Config.MAX_AGE_MINUTES:
            print(f"  [SKIP - too old] {p['title'][:50]} (posted {p['time_posted']} ago)")
            continue
        result.append(p)
    return result


# ============================
# EMAIL NOTIFICATIONS
# ============================
def format_posted_display(time_str):
    """Convert raw time_posted into a nice display string.
    '03/05/2026' → '2026/03/05 (0 DAYS AGO)'
    '03/04/2026' → '2026/03/04 (1 DAY AGO)'
    Relative strings ('2 hours ago') are returned as-is.
    """
    if not time_str or time_str == "Unknown":
        return time_str
    s = time_str.strip()
    # BTG absolute date: MM/DD/YYYY
    m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', s)
    if m:
        try:
            month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            posted_date = datetime(year, month, day, tzinfo=PKT).date()
            today = datetime.now(PKT).date()
            days_old = (today - posted_date).days
            label = f"{days_old} DAY{'S' if days_old != 1 else ''} AGO"
            return f"{year}/{month:02d}/{day:02d} ({label})"
        except Exception:
            pass
    # ISO YYYY-MM-DD
    m2 = re.search(r'(\d{4})-(\d{2})-(\d{2})', s)
    if m2:
        try:
            year, month, day = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
            posted_date = datetime(year, month, day, tzinfo=PKT).date()
            today = datetime.now(PKT).date()
            days_old = (today - posted_date).days
            label = f"{days_old} DAY{'S' if days_old != 1 else ''} AGO"
            return f"{year}/{month:02d}/{day:02d} ({label})"
        except Exception:
            pass
    # Relative string — return as-is
    return s


def fetch_project_details(driver, url):
    """Navigate to a BTG project detail page and extract full information."""
    details = {}
    try:
        driver.get(url)
        time.sleep(4)

        # Try CSS selectors for full description
        for sel in [".description", ".project-description", "[class*='description']",
                    ".overview", ".project-overview", ".summary", "[class*='overview']"]:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                t = el.text.strip()
                if len(t) > 50:
                    details["description"] = t
                    break
            except Exception:
                pass

        body_text = driver.find_element(By.TAG_NAME, "body").text
        # Normalize non-breaking spaces and Windows line endings
        body_text = body_text.replace('\u00a0', ' ').replace('\r\n', '\n').replace('\r', '\n')

        # Fallback: extract description block from body text
        if not details.get("description"):
            m = re.search(
                r'(?:Description|Overview|Summary)\s*\n([\s\S]+?)(?=\n(?:Project Details|Budget|Location|Requirements|Qualifications|Apply|Start Date)|\Z)',
                body_text, re.IGNORECASE
            )
            if m:
                txt = m.group(1).strip()
                if len(txt) > 50:
                    details["description"] = txt

        # Extract structured fields.
        # _SEP matches label→value separator in two formats:
        #   • same-line: "Timeline    6 months"  (spaces/tabs only)
        #   • next-line:  "Timeline\n6 months"   (newline, optional blank lines)
        _SEP = r'(?:[ \t]+|[ \t]*\n(?:[ \t]*\n)*[ \t]*)'
        patterns = {
            "start_date":       rf'(?:Start Date|Starts|Start:){_SEP}([^\n]{{2,60}})',
            "project_length":   rf'(?:Duration|Project Length|Expected Length){_SEP}([^\n]{{2,60}})',
            "timeline":         rf'Timeline{_SEP}([^\n]{{5,80}})',
            "engagement_type":  r'(?:Full time|Part time|Fractional)',
            "level_of_support": rf'Level of Support{_SEP}([^\n]{{2,60}})',
            "industry":         rf'(?:Industry|Desired Industry Background){_SEP}([^\n]{{2,100}})',
            "detail_budget":    rf'(?:Budget){_SEP}(\$[^\n]{{2,80}})',
            "deadline":         rf'Deadline{_SEP}([^\n]{{2,30}})',
            "location_type":    r'(On-site|Remote|Hybrid)',
        }
        for field, pattern in patterns.items():
            m = re.search(pattern, body_text, re.IGNORECASE)
            if m:
                val = (m.group(1) if m.lastindex else m.group(0)).strip()
                if val:
                    details[field] = val

        # Extract Requirements as a bullet list
        req_match = re.search(
            r'Requirements?\s*\n([\s\S]+?)(?=\n(?:Budget|Apply|Deadline|Not for you|\Z))',
            body_text, re.IGNORECASE
        )
        if req_match:
            lines = [l.strip() for l in req_match.group(1).splitlines() if l.strip()]
            if lines:
                details["requirements"] = lines

    except Exception as e:
        print(f"  ⚠️ Detail fetch failed: {e}")
    return details


def _esc(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _section_header(icon, title, color):
    return (
        f'<tr><td colspan="2" style="padding:14px 16px 6px;background:{color};'
        f'color:#fff;font-size:12px;font-weight:bold;'
        f'text-transform:uppercase;letter-spacing:1px;">'
        f'{icon}&nbsp; {title}</td></tr>'
    )


def _row(label, value, alt=False, bold_value=False):
    if not value:
        return ""
    bg   = "background:#f8f9fa;" if alt else "background:#fff;"
    bold = "font-weight:bold;" if bold_value else ""
    return (
        f"<tr>"
        f"<td style='padding:9px 16px;color:#555;width:200px;{bg}border-bottom:1px solid #eee;'>"
        f"<strong>{_esc(label)}</strong></td>"
        f"<td style='padding:9px 16px;{bg}{bold}border-bottom:1px solid #eee;'>{_esc(str(value))}</td>"
        f"</tr>"
    )


def create_email_html(project):
    title           = project.get("title", "Untitled Project")
    url             = project.get("url", Config.PROJECTS_URL)
    time_posted     = project.get("time_posted", "")
    status          = project.get("status", "")
    detected_at     = project.get("detected_at", "")
    project_id      = project.get("id", "")
    description     = project.get("description", "")
    location        = project.get("location", "") or "Remote / Not specified"
    location_type   = project.get("location_type", "")
    start_date      = project.get("start_date", "")
    timeline        = project.get("timeline", "")
    proj_length     = project.get("project_length", "") or project.get("duration", "")
    engagement_type = project.get("engagement_type", "")
    deadline        = project.get("deadline", "")
    budget          = project.get("budget", "") or project.get("detail_budget", "") or "Not provided"
    support_level   = project.get("level_of_support", "")
    industry        = project.get("industry", "")
    requirements    = project.get("requirements", [])  # list of strings

    hdr_grad   = "linear-gradient(135deg,#0e7490,#06b6d4)"
    sec_desc   = "#0e7490"
    sec_detail = "#155e75"
    sec_req    = "#1d4ed8"
    sec_budget = "#7c3aed"
    sec_meta   = "#6b7280"
    btn_color  = "#06b6d4"

    badge = ""
    if status == "New Project":
        badge = ("<span style='display:inline-block;background:#e74c3c;color:#fff;"
                 "padding:4px 12px;border-radius:3px;font-size:12px;font-weight:bold;"
                 "margin-bottom:12px;'>🆕 New Project</span>")

    # Description section
    desc_section = ""
    if description:
        paragraphs = _esc(description).replace("\n\n", "|||").replace("\n", " ")
        paras = [f"<p style='margin:0 0 10px;'>{p}</p>" for p in paragraphs.split("|||")]
        desc_section = (
            _section_header('📋', 'Description', sec_desc) +
            f"<tr><td colspan='2' style='padding:14px 16px;background:#f9fafb;"
            f"font-size:14px;line-height:1.75;color:#333;border-bottom:2px solid #e5e7eb;'>"
            f"{''.join(paras)}</td></tr>"
        )

    # Project Details section
    # Build timeline display: prefer explicit timeline, fall back to start_date
    timeline_display = timeline or start_date or "TBD"
    if proj_length and proj_length not in timeline_display:
        timeline_display += f"  ({proj_length})"
    loc_display = location
    if location_type and location_type.lower() not in location.lower():
        loc_display += f" — {location_type}"

    detail_rows = (
        _row("Location",    loc_display,                                     alt=False) +
        _row("Timeline",    timeline_display,                                alt=True) +
        _row("Engagement",  engagement_type or "Not specified",              alt=False) +
        _row("Deadline",    deadline,                                        alt=True)
    )
    detail_section = _section_header('📦', 'Project Details', sec_detail) + detail_rows

    # Requirements section (bullet list)
    req_section = ""
    if requirements:
        items = "".join(
            f"<li style='margin-bottom:6px;'>{_esc(r)}</li>" for r in requirements
        )
        req_section = (
            _section_header('✅', 'Requirements', sec_req) +
            f"<tr><td colspan='2' style='padding:14px 16px;background:#f8f9fa;"
            f"font-size:14px;line-height:1.6;color:#333;border-bottom:2px solid #e5e7eb;'>"
            f"<ul style='margin:0;padding-left:20px;'>{items}</ul></td></tr>"
        )

    # Budget section
    budget_section = (
        _section_header('💰', 'Budget', sec_budget) +
        _row("Rate / Budget", budget, bold_value=bool(project.get("budget") or project.get("detail_budget")))
    )
    if support_level or industry:
        budget_section += (
            _row("Level of Support", support_level or "Not specified", alt=True) +
            _row("Industry",         industry       or "Not specified", alt=False)
        )

    # Detection meta
    time_posted_display = format_posted_display(time_posted) if time_posted else "—"
    meta_rows = (
        _row("Posted",      time_posted_display,  alt=False) +
        _row("Detected at", detected_at,          alt=True) +
        _row("Project ID",  project_id,           alt=False)
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:Arial,Helvetica,sans-serif;color:#333;">
  <div style="max-width:700px;margin:30px auto;background:#fff;border-radius:10px;
       overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,0.12);">

    <div style="background:{hdr_grad};padding:24px 28px;">
      <p style="margin:0;color:rgba(255,255,255,0.75);font-size:11px;
          letter-spacing:1.5px;text-transform:uppercase;">BTG Project Monitor</p>
      <h2 style="margin:6px 0 0;color:#fff;font-size:24px;font-weight:700;">🚀 New BTG Project Alert</h2>
    </div>

    <div style="padding:22px 28px 4px;">
      <h3 style="margin:0 0 10px;color:#1a252f;font-size:20px;line-height:1.4;">{_esc(title)}</h3>
      {badge}
    </div>

    <div style="padding:0 28px 28px;">
      <table style="width:100%;border-collapse:collapse;font-size:14px;
             border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
        {desc_section}
        {detail_section}
        {req_section}
        {budget_section}
        {_section_header('🕒', 'Detection Info', sec_meta)}
        {meta_rows}
      </table>
      <div style="text-align:center;margin-top:28px;">
        <a href="{url}" style="display:inline-block;background:{btn_color};color:#fff;
                  padding:14px 36px;text-decoration:none;border-radius:6px;
                  font-weight:bold;font-size:15px;letter-spacing:0.3px;">
          View Full Project on BTG →
        </a>
      </div>
    </div>

    <div style="background:#f8f9fa;padding:14px 28px;border-top:1px solid #eee;
         font-size:12px;color:#999;text-align:center;">
      BTG Project Monitor &nbsp;|&nbsp; Automated alert &nbsp;|&nbsp; {detected_at}
    </div>
  </div>
</body></html>"""


def send_notification(project):
    """Send email alert for a new BTG project."""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"🔔 BTG: {project.get('title', 'New Project')}"
        msg["From"]    = Config.SENDER_EMAIL
        msg["To"]      = ", ".join(Config.RECIPIENT_EMAILS)
        msg.attach(MIMEText(create_email_html(project), "html"))

        with smtplib.SMTP(Config.SMTP_SERVER, Config.SMTP_PORT) as server:
            server.starttls()
            server.login(Config.SENDER_EMAIL, Config.SENDER_PASSWORD)
            server.send_message(msg)

        print(f"📧 Email sent: {project.get('title', 'Unknown')[:50]}...")
        return True
    except Exception as e:
        print(f"❌ Email failed: {e}")
        return False


# ============================
# DRIVER + SESSION SETUP
# ============================
def _find_binary(env_var, candidates):
    """Return the first existing path from env var or candidate list."""
    import shutil
    val = os.getenv(env_var, "")
    if val and os.path.exists(val):
        return val
    for path in candidates:
        if os.path.exists(path):
            return path
    found = shutil.which(candidates[-1].split('/')[-1])
    return found or ""


def initialize_driver():
    options = Options()
    if Config.HEADLESS:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

    chrome_bin = _find_binary("CHROME_BIN", [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ])
    if chrome_bin:
        options.binary_location = chrome_bin
        print(f"  Chrome binary: {chrome_bin}")

    from selenium.webdriver.chrome.service import Service

    # Primary: system chromedriver — apt installs chromium-driver version-matched to chromium
    system_path = _find_binary("CHROMEDRIVER_PATH", [
        "/usr/bin/chromedriver",
        "/usr/lib/chromium/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
    ])
    if system_path:
        service = Service(system_path)
        print(f"  Chromedriver (system): {system_path}")
    else:
        # Fallback: webdriver-manager (downloads matching chromedriver)
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            from webdriver_manager.core.os_manager import ChromeType
            is_chromium = "chromium" in (chrome_bin or "").lower()
            mgr = ChromeDriverManager(chrome_type=ChromeType.CHROMIUM if is_chromium else ChromeType.GOOGLE)
            driver_path = mgr.install()
            service = Service(driver_path)
            print(f"  Chromedriver (webdriver-manager): {driver_path}")
        except Exception as e:
            print(f"  Using default Service(): {e}")
            service = Service()

    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_cdp_cmd("Network.setUserAgentOverride", {
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    return driver


def setup_session(driver):
    """Try cookies first, fall back to login."""
    if load_cookies(driver):
        driver.get(Config.PROJECTS_URL)
        time.sleep(5)
        # Check we're not kicked back to login
        if "login" not in driver.current_url.lower() and "sign" not in driver.current_url.lower():
            print("✅ Logged in via cookies")
            return True
        print("  Cookies expired — logging in fresh...")

    return perform_login(driver)


# ============================
# MAIN MONITORING LOOP
# ============================
def main():
    print("=" * 50)
    print("🚀 BTG Project Monitor")
    if DEBUG_MODE:
        print("   (DEBUG MODE ON — page structure will be printed)")
    print("=" * 50)
    print(f"  Account  : {Config.BTG_EMAIL}")
    print(f"  Interval : {Config.CHECK_INTERVAL}s")
    print(f"  Max age  : {Config.MAX_AGE_MINUTES} min")
    print(f"  Recipients: {', '.join(Config.RECIPIENT_EMAILS)}")
    print()

    if TEST_MODE:
        Config.RECIPIENT_EMAILS = ["muhammadammar7747@gmail.com"]
        print("🧪 TEST MODE — MongoDB skipped, 1 test email → muhammadammar7747@gmail.com\n")

    driver = initialize_driver()

    try:
        if not setup_session(driver):
            print("❌ Failed to establish BTG session — retrying in 60s...")
            try:
                driver.quit()
            except Exception:
                pass
            time.sleep(60)
            return  # outer restart loop will call main() again

        # After login, navigate to projects
        driver.get(Config.PROJECTS_URL)
        time.sleep(4)

        if TEST_MODE:
            seen_ids = set()
            print("🧪 DB skipped — running in-memory only\n")
        else:
            cold_start = db_is_cold_start()
            init_db()
            seen_ids = get_seen_ids()
            print(f"📁 DB loaded — {len(seen_ids)} projects on record\n")

            # ── COLD START: DB didn't exist → seed silently, no emails ────────────
            if cold_start:
                print("⚙️  First run detected — seeding existing projects (no emails will be sent)...")
                seed_projects = scan_for_projects(driver)
                if seed_projects:
                    bulk_insert_projects(seed_projects, emailed=False)
                    print(f"✅ Seeded {len(seed_projects)} existing projects. Only NEW posts from now on will trigger emails.\n")
                    seen_ids = get_seen_ids()
                else:
                    print("⚠️  Could not seed projects on first run — will try again next cycle.\n")
            # ─────────────────────────────────────────────────────────────────────

        check_count = 0
        last_keepalive = time.time()
        KEEPALIVE_INTERVAL = 1800  # refresh session every 30 minutes
        while True:
          try:
            check_count += 1
            print(f"\n{'='*30}")
            print(f"🔄 Check #{check_count} — {datetime.now(PKT).strftime('%H:%M:%S')} PKT")
            print(f"{'='*30}")

            # Keep-alive: re-save cookies every 30 min to reset expiry in MongoDB
            if time.time() - last_keepalive > KEEPALIVE_INTERVAL:
                save_cookies(driver)
                last_keepalive = time.time()
                print("  🔁 Session keep-alive: cookies refreshed")

            driver.get(Config.PROJECTS_URL)
            time.sleep(5)

            # If session expired, BTG silently redirects to /login — re-login immediately
            if "login" in driver.current_url.lower() or "sign" in driver.current_url.lower():
                print("  ⚠️ Session expired — re-logging in...")
                if not perform_login(driver):
                    print("  ❌ Re-login failed — skipping cycle")
                    time.sleep(Config.CHECK_INTERVAL)
                    continue
                driver.get(Config.PROJECTS_URL)
                time.sleep(5)

            all_projects = scan_for_projects(driver)

            if not all_projects:
                print("⚠️  No projects extracted this cycle")
                if ONCE_MODE:
                    break
                time.sleep(Config.CHECK_INTERVAL)
                continue

            new_projects = filter_new_projects(all_projects, seen_ids)

            if TEST_MODE and all_projects and not seen_ids:
                # First cycle: send exactly 1 test email, then mark everything seen
                project = all_projects[0]
                print(f"🧪 TEST: Sending 1 test email → {project['title'][:60]}...")
                send_notification(project)
                for p in all_projects:
                    seen_ids.add(p["id"])
            elif new_projects:
                print(f"🎯 Found {len(new_projects)} NEW project(s)!")
                for project in new_projects:
                    print(f"  → {project['title'][:60]}...")
                    print(f"     Fetching full project details...")
                    details = fetch_project_details(driver, project['url'])
                    project.update(details)
                    emailed = send_notification(project)
                    if not TEST_MODE:
                        insert_project(project, emailed=emailed)
                    seen_ids.add(project['id'])
            else:
                print("⏳ No new projects this cycle")

            print(f"📊 Stats: {len(all_projects)} visible, {len(seen_ids)} in DB")

            if ONCE_MODE:
                print("\n✅ Once mode complete. Exiting.")
                break

            print(f"\n⏳ Next check in {Config.CHECK_INTERVAL}s...")
            time.sleep(Config.CHECK_INTERVAL)

          except KeyboardInterrupt:
            raise
          except Exception as loop_err:
            print(f"⚠️ Check failed: {loop_err} — retrying in {Config.CHECK_INTERVAL}s...")
            try:
                driver.quit()
            except Exception:
                pass
            time.sleep(Config.CHECK_INTERVAL)
            driver = initialize_driver()
            if not setup_session(driver):
                print("❌ Re-login failed — will retry next cycle")

    except KeyboardInterrupt:
        raise  # let outer loop handle clean exit
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        print("✅ BTG Monitor stopped")


if __name__ == "__main__":
    while True:
        try:
            main()
            if ONCE_MODE:
                break
            # main() returned without ONCE_MODE — unexpected, restart after delay
            print("⚠️  Monitor exited unexpectedly — restarting in 30s...")
            time.sleep(30)
        except KeyboardInterrupt:
            print("\n⏹️  Stopped by user")
            break
        except Exception as fatal:
            print(f"💥 Fatal crash: {fatal} — restarting in 30s...")
            time.sleep(30)
