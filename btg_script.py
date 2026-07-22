import time
import smtplib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import traceback as traceback_mod
from pymongo import MongoClient, UpdateOne
from datetime import datetime, timezone, timedelta
from email import encoders
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, ElementClickInterceptedException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
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
    _ERROR_RECIPIENTS_RAW = (
        os.getenv("error_recipent")
        or os.getenv("ERROR_RECIPENT")
        or os.getenv("ERROR_RECIPIENT")
        or os.getenv("ERROR_RECIPIENTS")
        or ""
    )
    ERROR_RECIPIENTS = [
        email.strip()
        for email in _ERROR_RECIPIENTS_RAW.split(",")
        if email.strip()
    ]
    ERROR_EMAIL_COOLDOWN_MINUTES = int(
        os.getenv("ERROR_EMAIL_COOLDOWN_MINUTES", "30")
    )
    LOGIN_RETRY_INTERVAL = int(os.getenv("LOGIN_RETRY_INTERVAL", "300"))
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
TEST_ERROR_EMAIL_MODE = "--test-error-email" in sys.argv
CHROMEDRIVER_LOG_PATH = "/tmp/chromedriver.log"

# Runtime state for operational alerts (never stores secrets)
_error_email_last_sent = {}
_sending_error_email = False
_monitor_check_count = 0
_zero_project_streak = 0
_browser_versions_cache = None

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
# LOGIN RESULT + ERROR ALERTS
# ============================
class LoginResult:
    SUCCESS = "SUCCESS"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    MFA_REQUIRED = "MFA_REQUIRED"
    LOGIN_TIMEOUT = "LOGIN_TIMEOUT"
    LOGIN_PAGE_CHANGED = "LOGIN_PAGE_CHANGED"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    UNKNOWN_LOGIN_FAILURE = "UNKNOWN_LOGIN_FAILURE"

    AUTH_BLOCKERS = {
        INVALID_CREDENTIALS,
        CAPTCHA_REQUIRED,
        MFA_REQUIRED,
        LOGIN_TIMEOUT,
        LOGIN_PAGE_CHANGED,
        CONFIGURATION_ERROR,
        UNKNOWN_LOGIN_FAILURE,
    }

    def __init__(self, status, message="", details=None):
        self.status = status
        self.message = message or ""
        self.details = details or {}

    @property
    def ok(self):
        return self.status == self.SUCCESS

    def __bool__(self):
        return self.ok


def _safe_quit(driver):
    if not driver:
        return
    try:
        driver.quit()
    except Exception:
        pass


def _evidence_dir():
    for path in ("/tmp", tempfile.gettempdir()):
        try:
            os.makedirs(path, exist_ok=True)
            return path
        except Exception:
            continue
    return tempfile.gettempdir()


def get_browser_versions():
    """Cached Chromium / ChromeDriver version strings for diagnostics."""
    global _browser_versions_cache
    if _browser_versions_cache is None:
        _browser_versions_cache = {
            "chromium": _run_command_for_diagnostic(["chromium", "--version"]),
            "chromedriver": _run_command_for_diagnostic(["chromedriver", "--version"]),
        }
        # Fallbacks when `chromium` binary name differs
        if _browser_versions_cache["chromium"] in ("not found", "failed"):
            for cmd in (["chromium-browser", "--version"], ["google-chrome", "--version"]):
                out = _run_command_for_diagnostic(cmd)
                if out not in ("not found",) and not str(out).startswith("failed"):
                    _browser_versions_cache["chromium"] = out
                    break
    return _browser_versions_cache


def _error_cooldown_key(context, error):
    err_type = type(error).__name__ if error is not None and not isinstance(error, str) else "Error"
    err_msg = str(error) if error is not None else ""
    return f"{context}|{err_type}|{err_msg[:200]}"


def _html_esc(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def create_error_email_html(
    context,
    error,
    details="",
    traceback_text="",
    extra_rows=None,
):
    err_type = type(error).__name__ if error is not None and not isinstance(error, str) else "Error"
    err_msg = _html_esc(str(error) if error is not None else "")
    versions = get_browser_versions()
    hostname = socket.gethostname()
    now = datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S PKT")
    rows = [
        ("Context", _html_esc(context)),
        ("Exception", f"{_html_esc(err_type)}: {err_msg}"),
        ("Timestamp", _html_esc(now)),
        ("Hostname", _html_esc(hostname)),
        ("Check #", str(_monitor_check_count or "—")),
        ("Headless", str(Config.HEADLESS)),
        ("Chromium", _html_esc(versions.get("chromium", "unknown"))),
        ("ChromeDriver", _html_esc(versions.get("chromedriver", "unknown"))),
    ]
    for label, value in (extra_rows or []):
        if value is not None and str(value) != "":
            rows.append((label, _html_esc(str(value))))
    if details:
        rows.append(("Details", f"<pre style='white-space:pre-wrap;margin:0;font-size:12px;'>{_html_esc(details)}</pre>"))
    if traceback_text:
        rows.append((
            "Traceback",
            f"<pre style='white-space:pre-wrap;margin:0;font-size:11px;color:#7f1d1d;'>"
            f"{_html_esc(traceback_text[:8000])}</pre>",
        ))

    body_rows = "".join(
        f"<tr>"
        f"<td style='padding:10px 14px;width:180px;background:#fef2f2;border-bottom:1px solid #fecaca;"
        f"font-weight:bold;color:#7f1d1d;vertical-align:top;'>{label}</td>"
        f"<td style='padding:10px 14px;border-bottom:1px solid #fecaca;color:#111;vertical-align:top;'>{value}</td>"
        f"</tr>"
        for label, value in rows
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;">
  <div style="max-width:720px;margin:24px auto;background:#fff;border-radius:8px;overflow:hidden;
       box-shadow:0 4px 14px rgba(0,0,0,0.12);">
    <div style="background:linear-gradient(135deg,#b91c1c,#ef4444);padding:20px 24px;color:#fff;">
      <p style="margin:0;font-size:11px;letter-spacing:1px;text-transform:uppercase;opacity:0.85;">
        BTG Project Monitor</p>
      <h2 style="margin:6px 0 0;font-size:22px;">Operational Error Alert</h2>
    </div>
    <div style="padding:18px 20px 24px;">
      <table style="width:100%;border-collapse:collapse;border:1px solid #fecaca;">{body_rows}</table>
      <p style="margin:16px 0 0;font-size:12px;color:#6b7280;">
        This alert was sent only to configured error recipients. Passwords and tokens are never included.
      </p>
    </div>
  </div>
</body></html>"""


def send_error_notification(
    context,
    error,
    details="",
    traceback_text="",
    attachments=None,
    force=False,
    extra_rows=None,
):
    """Send an operational error email to Config.ERROR_RECIPIENTS only."""
    global _sending_error_email, _error_email_last_sent

    if _sending_error_email:
        print("  ⚠️ Error-email function failed recursively — alert suppressed")
        return False

    if not Config.ERROR_RECIPIENTS:
        print("  ⚠️ Error alert skipped — no error_recipent / ERROR_RECIPIENTS configured")
        return False

    if not Config.SENDER_EMAIL or not Config.SENDER_PASSWORD:
        print("  ⚠️ Error alert skipped — SENDER_EMAIL / SENDER_PASSWORD not configured")
        return False

    key = _error_cooldown_key(context, error)
    now = time.time()
    cooldown_s = max(Config.ERROR_EMAIL_COOLDOWN_MINUTES, 0) * 60
    if not force and key in _error_email_last_sent:
        elapsed = now - _error_email_last_sent[key]
        if elapsed < cooldown_s:
            remaining = int(cooldown_s - elapsed)
            print(f"  ⏳ Error alert suppressed (cooldown {remaining}s remaining): {context}")
            return False

    _sending_error_email = True
    try:
        html = create_error_email_html(
            context, error, details=details, traceback_text=traceback_text, extra_rows=extra_rows
        )
        msg = MIMEMultipart("mixed")
        err_label = type(error).__name__ if error is not None and not isinstance(error, str) else "Error"
        msg["Subject"] = f"🚨 BTG Monitor Error: {context} ({err_label})"
        msg["From"] = Config.SENDER_EMAIL
        msg["To"] = ", ".join(Config.ERROR_RECIPIENTS)
        msg.attach(MIMEText(html, "html"))

        attached = []
        for path in attachments or []:
            if not path or not os.path.isfile(path):
                continue
            try:
                with open(path, "rb") as fh:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(fh.read())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition",
                    f'attachment; filename="{os.path.basename(path)}"',
                )
                msg.attach(part)
                attached.append(os.path.basename(path))
            except Exception as attach_err:
                print(f"  ⚠️ Could not attach {path}: {attach_err}")

        with smtplib.SMTP(Config.SMTP_SERVER, Config.SMTP_PORT) as server:
            server.starttls()
            server.login(Config.SENDER_EMAIL, Config.SENDER_PASSWORD)
            server.send_message(msg)

        _error_email_last_sent[key] = now
        print(f"📧 Error alert sent to configured error recipient"
              f"{' (attachments: ' + ', '.join(attached) + ')' if attached else ''}")
        return True
    except Exception as smtp_err:
        print(f"❌ Error alert email failed: {smtp_err}")
        return False
    finally:
        _sending_error_email = False


def run_test_error_email():
    """Send one forced test alert and exit (no Selenium / Mongo)."""
    print("=" * 50)
    print("🧪 BTG Monitor — test error email")
    print("=" * 50)
    if Config.ERROR_RECIPIENTS:
        print(f"  Error alerts: {', '.join(Config.ERROR_RECIPIENTS)}")
    else:
        print("  Error alerts: NOT CONFIGURED (set error_recipent)")
        print("❌ Cannot send test — configure error_recipent first")
        return False
    print(f"  Error cooldown: {Config.ERROR_EMAIL_COOLDOWN_MINUTES} minutes")
    ok = send_error_notification(
        "TEST_ERROR_EMAIL",
        "Forced test alert from BTG Project Monitor",
        details=(
            "This is a forced test of send_error_notification(). "
            "No Selenium browser or MongoDB connection was opened."
        ),
        force=True,
        extra_rows=[
            ("Mode", "--test-error-email"),
            ("Script", os.path.basename(__file__)),
        ],
    )
    print("✅ Test error email sent" if ok else "❌ Test error email failed")
    return ok


def save_login_failure_evidence(driver, prefix="btg_login_failure"):
    """Save screenshot + HTML evidence. Returns (png_path, html_path)."""
    ts = datetime.now(PKT).strftime("%Y%m%d_%H%M%S")
    base = os.path.join(_evidence_dir(), f"{prefix}_{ts}")
    png_path = f"{base}.png"
    html_path = f"{base}.html"
    try:
        driver.save_screenshot(png_path)
        print(f"  Saved login failure screenshot: {png_path}")
    except Exception as e:
        print(f"  ⚠️ Screenshot failed: {e}")
        png_path = ""
    try:
        with open(html_path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(driver.page_source or "")
        print(f"  Saved login failure HTML: {html_path}")
    except Exception as e:
        print(f"  ⚠️ HTML capture failed: {e}")
        html_path = ""
    return png_path, html_path


def _dispatch_angular_events(driver, element):
    driver.execute_script(
        """
        var el = arguments[0];
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new Event('blur', { bubbles: true }));
        """,
        element,
    )


def _fill_input_field(driver, element, value, label, is_password=False):
    """Click, clear via Ctrl+A/Backspace, type, fire Angular events, verify value."""
    WebDriverWait(driver, 10).until(lambda d: element.is_displayed() and element.is_enabled())
    element.click()
    time.sleep(0.2)
    try:
        element.send_keys(Keys.CONTROL, "a")
        element.send_keys(Keys.BACKSPACE)
    except Exception:
        try:
            element.clear()
        except Exception:
            pass
    element.send_keys(value)
    _dispatch_angular_events(driver, element)
    time.sleep(0.3)
    actual = element.get_attribute("value") or ""
    if is_password:
        print(f"  Password field populated: {len(actual)} characters.")
        if len(actual) != len(value):
            print(f"  ⚠️ Password length mismatch (expected {len(value)}, got {len(actual)})")
            return False
    else:
        print(f"  {label} field populated.")
        if actual.strip() != str(value).strip():
            print(f"  ⚠️ {label} value mismatch after fill")
            return False
    return True


def _find_login_submit_button(driver):
    selectors = [
        'button[type="submit"]',
        'input[type="submit"]',
        "button.mat-mdc-raised-button",
        "button.mat-raised-button",
        "button",
    ]
    keywords = ("sign in", "login", "log in", "continue")
    for sel in selectors:
        try:
            elems = driver.find_elements(By.CSS_SELECTOR, sel)
        except Exception:
            continue
        for btn in elems:
            try:
                if not btn.is_displayed():
                    continue
                if sel == "button":
                    text = (btn.text or "").strip().lower()
                    aria = (btn.get_attribute("aria-label") or "").strip().lower()
                    if not any(k in text or k in aria for k in keywords):
                        continue
                return btn
            except Exception:
                continue
    return None


def _collect_visible_login_errors(driver):
    selectors = [
        '[role="alert"]',
        ".alert",
        ".alert-danger",
        ".error",
        ".error-message",
        ".validation-error",
        "mat-error",
        ".mat-mdc-form-field-error",
        ".snackbar",
        ".mat-mdc-snack-bar-label",
    ]
    found = []
    for sel in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                if el.is_displayed():
                    txt = (el.text or "").strip()
                    if txt and txt not in found:
                        found.append(txt)
        except Exception:
            pass
    return " | ".join(found)


def _safe_page_text(driver, limit=2000):
    try:
        text = driver.find_element(By.TAG_NAME, "body").text or ""
    except Exception:
        text = ""
    # Strip anything that looks like an email password field value is already not in body.text usually
    return text[:limit]


def _classify_login_outcome(driver):
    """Return (status, message) or (None, '') if still indeterminate."""
    url = (driver.current_url or "").lower()
    try:
        title = driver.title or ""
    except Exception:
        title = ""
    body = _safe_page_text(driver, 4000).lower()
    error_text = _collect_visible_login_errors(driver)

    left_login = (
        "login" not in url
        and "sign-in" not in url
        and "signin" not in url
        and "/sign" not in url
    )
    if left_login:
        return LoginResult.SUCCESS, "Redirected away from login"

    # Logged-in project-page markers while URL still resolving
    for sel in (
        "div.detail.date-posted",
        ".detail.date-posted",
        "div.detail.location",
        "div.detail.budget",
        "[class*='project-card']",
    ):
        try:
            if driver.find_elements(By.CSS_SELECTOR, sel):
                return LoginResult.SUCCESS, f"Logged-in element present ({sel})"
        except Exception:
            pass

    captcha_phrases = ("captcha", "verify you are human", "recaptcha", "hcaptcha", "bot detection")
    if any(p in body for p in captcha_phrases) or "captcha" in url:
        return LoginResult.CAPTCHA_REQUIRED, "CAPTCHA / bot verification detected — manual action required"

    mfa_phrases = (
        "verification code", "two-factor", "multi-factor", "2-factor",
        "one-time password", "one time password", "authenticator", "enter the code",
    )
    if any(p in body for p in mfa_phrases):
        return LoginResult.MFA_REQUIRED, "MFA / verification code page detected — manual action required"

    lock_phrases = ("access denied", "account locked", "too many attempts", "temporarily locked")
    if any(p in body for p in lock_phrases):
        return LoginResult.INVALID_CREDENTIALS, error_text or "Account locked or access denied"

    cred_phrases = (
        "invalid", "incorrect", "authentication failed", "unable to sign in",
        "wrong password", "wrong email", "credentials",
    )
    if error_text or any(p in body for p in cred_phrases):
        return LoginResult.INVALID_CREDENTIALS, error_text or "Invalid credentials indicated on page"

    if "login" in url or "sign" in url:
        return None, title
    return LoginResult.LOGIN_PAGE_CHANGED, f"Unexpected post-login URL/title: {url} / {title}"


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
        send_error_notification(
            "COOKIE_SAVE_FAILURE",
            e,
            details="Failed to persist BTG session cookies to MongoDB.",
            traceback_text=traceback_mod.format_exc(),
        )
    # Local file fallback
    try:
        path = os.path.join(os.path.dirname(__file__), Config.COOKIES_FILE)
        with open(path, 'w') as f:
            json.dump(cookies, f)
    except Exception as e:
        print(f"  ⚠️ Could not save local cookie backup: {e}")
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
        send_error_notification(
            "COOKIE_LOAD_FAILURE",
            e,
            details="Failed to load BTG session cookies from MongoDB.",
            traceback_text=traceback_mod.format_exc(),
        )
    # Fall back to local file
    if not cookies:
        path = os.path.join(os.path.dirname(__file__), Config.COOKIES_FILE)
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    cookies = json.load(f)
                print("  Loaded cookies from local file")
            except Exception as e:
                print(f"  ⚠️ Could not load local cookie backup: {e}")
                send_error_notification(
                    "COOKIE_LOAD_FAILURE",
                    e,
                    details="Failed to load BTG session cookies from local backup file.",
                )
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
    except Exception as e:
        print(f"  ⚠️ Failed applying cookies to browser: {e}")
        send_error_notification(
            "COOKIE_LOAD_FAILURE",
            e,
            details="Cookies were loaded but could not be applied to the browser session.",
            traceback_text=traceback_mod.format_exc(),
        )
        return False


def clear_stale_cookies():
    """Remove expired cookie records so they are not reloaded on every restart."""
    try:
        _get_session_collection().delete_one({"_id": "btg_cookies"})
        print("  Cleared stale cookies from MongoDB")
    except Exception as e:
        print(f"  ⚠️ Could not clear MongoDB cookies: {e}")
        send_error_notification(
            "COOKIE_CLEAR_FAILURE",
            e,
            details="Failed to delete expired BTG cookies from MongoDB.",
        )
    try:
        path = os.path.join(os.path.dirname(__file__), Config.COOKIES_FILE)
        if os.path.exists(path):
            os.remove(path)
            print("  Cleared stale local cookie backup")
    except Exception as e:
        print(f"  ⚠️ Could not clear local cookie backup: {e}")


def _login_failure_alert(driver, result, diagnostics):
    """Persist evidence and email a detailed login failure (cooldown applies)."""
    png_path, html_path = "", ""
    if driver:
        try:
            png_path, html_path = save_login_failure_evidence(driver)
        except Exception as e:
            print(f"  ⚠️ Evidence capture failed: {e}")

    page_text = ""
    current_url = ""
    page_title = ""
    if driver:
        try:
            current_url = driver.current_url
        except Exception:
            pass
        try:
            page_title = driver.title
        except Exception:
            pass
        page_text = _safe_page_text(driver, 2000)

    details_parts = [
        f"Login result: {result.status}",
        f"Message: {result.message}",
        f"Email field found: {diagnostics.get('email_found')}",
        f"Password field found: {diagnostics.get('password_found')}",
        f"Submit button found: {diagnostics.get('submit_found')}",
        f"Submit button enabled: {diagnostics.get('submit_enabled')}",
        f"Form submission attempted: {diagnostics.get('submitted')}",
        f"CAPTCHA detected: {result.status == LoginResult.CAPTCHA_REQUIRED}",
        f"MFA detected: {result.status == LoginResult.MFA_REQUIRED}",
        f"Screenshot: {png_path or 'n/a'}",
        f"HTML: {html_path or 'n/a'}",
        "",
        "Visible page text (truncated, no secrets):",
        page_text or "(unavailable)",
    ]
    attachments = [p for p in (png_path, html_path) if p]
    send_error_notification(
        f"LOGIN_FAILURE:{result.status}",
        result.message or result.status,
        details="\n".join(details_parts),
        attachments=attachments,
        extra_rows=[
            ("Current URL", current_url),
            ("Page title", page_title),
            ("Visible login error", diagnostics.get("visible_error") or result.message),
            ("Email field found", diagnostics.get("email_found")),
            ("Password field found", diagnostics.get("password_found")),
            ("Submit found/enabled",
             f"{diagnostics.get('submit_found')} / {diagnostics.get('submit_enabled')}"),
            ("Submitted", diagnostics.get("submitted")),
            ("Evidence PNG", png_path or "n/a"),
            ("Evidence HTML", html_path or "n/a"),
        ],
    )


def perform_login(driver):
    """Log in to BTG with Angular-aware form fill and classified outcomes."""
    diagnostics = {
        "email_found": False,
        "password_found": False,
        "submit_found": False,
        "submit_enabled": False,
        "submitted": False,
        "visible_error": "",
    }

    missing = []
    if not (Config.BTG_EMAIL or "").strip():
        missing.append("BTG_EMAIL")
    if not (Config.BTG_PASSWORD or "").strip():
        missing.append("BTG_PASSWORD")
    if missing:
        msg = f"Missing required environment variable(s): {', '.join(missing)}"
        print(f"❌ {msg}")
        result = LoginResult(LoginResult.CONFIGURATION_ERROR, msg)
        send_error_notification(
            "LOGIN_CONFIGURATION_ERROR",
            msg,
            details="BTG login aborted before opening the browser login page. Password contents are never logged.",
            extra_rows=[("Missing variables", ", ".join(missing))],
        )
        return result

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

        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(1)
        print("  Login form detected.")

        # --- email field ---
        email_field = None
        for sel in [
            'input[type="email"]',
            'input[name="email"]',
            'input[id*="email"]',
            'input[placeholder*="email" i]',
            'input[formcontrolname="email"]',
        ]:
            try:
                email_field = WebDriverWait(driver, 8).until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, sel))
                )
                if email_field.is_enabled():
                    break
                email_field = None
            except TimeoutException:
                continue

        if not email_field:
            print("❌ Could not find email field.")
            dump_page_structure(driver)
            result = LoginResult(
                LoginResult.LOGIN_PAGE_CHANGED,
                "Email field not found — login page structure may have changed",
            )
            _login_failure_alert(driver, result, diagnostics)
            return result

        diagnostics["email_found"] = True
        if not _fill_input_field(driver, email_field, Config.BTG_EMAIL, "Email"):
            result = LoginResult(
                LoginResult.UNKNOWN_LOGIN_FAILURE,
                "Failed to populate email field with expected value",
            )
            _login_failure_alert(driver, result, diagnostics)
            return result

        # --- password field ---
        password_field = None
        for sel in [
            'input[type="password"]',
            'input[name="password"]',
            'input[id*="password"]',
            'input[formcontrolname="password"]',
        ]:
            try:
                password_field = WebDriverWait(driver, 5).until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, sel))
                )
                if password_field.is_enabled():
                    break
                password_field = None
            except (TimeoutException, NoSuchElementException):
                continue

        if not password_field:
            print("❌ Could not find password field.")
            result = LoginResult(
                LoginResult.LOGIN_PAGE_CHANGED,
                "Password field not found — login page structure may have changed",
            )
            _login_failure_alert(driver, result, diagnostics)
            return result

        diagnostics["password_found"] = True
        if not _fill_input_field(
            driver, password_field, Config.BTG_PASSWORD, "Password", is_password=True
        ):
            result = LoginResult(
                LoginResult.UNKNOWN_LOGIN_FAILURE,
                "Failed to populate password field (length mismatch)",
            )
            _login_failure_alert(driver, result, diagnostics)
            return result

        # --- submit button (prefer button click over Enter) ---
        submit_btn = _find_login_submit_button(driver)
        if not submit_btn:
            print("❌ Could not find submit button.")
            result = LoginResult(
                LoginResult.LOGIN_PAGE_CHANGED,
                "Submit button not found on login form",
            )
            _login_failure_alert(driver, result, diagnostics)
            return result

        diagnostics["submit_found"] = True
        try:
            disabled = submit_btn.get_attribute("disabled")
            aria_disabled = (submit_btn.get_attribute("aria-disabled") or "").lower()
            is_disabled = disabled is not None or aria_disabled == "true"
        except Exception:
            is_disabled = False

        if is_disabled:
            print("  Submit button found: disabled — waiting for enable...")
            try:
                WebDriverWait(driver, 10).until(
                    lambda d: submit_btn.get_attribute("disabled") is None
                    and (submit_btn.get_attribute("aria-disabled") or "").lower() != "true"
                )
                is_disabled = False
            except TimeoutException:
                is_disabled = True

        diagnostics["submit_enabled"] = not is_disabled
        print(f"  Submit button found: {'enabled' if not is_disabled else 'disabled'}.")

        if is_disabled:
            result = LoginResult(
                LoginResult.UNKNOWN_LOGIN_FAILURE,
                "Submit button remained disabled after filling credentials",
            )
            diagnostics["visible_error"] = _collect_visible_login_errors(driver)
            _login_failure_alert(driver, result, diagnostics)
            return result

        try:
            submit_btn.click()
            print("  Login submitted by button click.")
        except (ElementClickInterceptedException, Exception) as click_err:
            print(f"  Normal click failed ({type(click_err).__name__}) — trying JS click")
            try:
                driver.execute_script("arguments[0].click();", submit_btn)
                print("  Login submitted by JS button click.")
            except Exception as js_err:
                result = LoginResult(
                    LoginResult.UNKNOWN_LOGIN_FAILURE,
                    f"Could not click submit button: {js_err}",
                )
                _login_failure_alert(driver, result, diagnostics)
                return result

        diagnostics["submitted"] = True

        # Wait up to 30s for a classified outcome
        deadline = time.time() + 30
        last_status = None
        last_message = ""
        while time.time() < deadline:
            status, message = _classify_login_outcome(driver)
            if status == LoginResult.SUCCESS:
                save_cookies(driver)
                print(f"  Login result: {LoginResult.SUCCESS}")
                print(f"✅ Login successful → {driver.current_url}")
                return LoginResult(LoginResult.SUCCESS, message)
            if status is not None:
                last_status, last_message = status, message
                break
            time.sleep(0.5)
        else:
            last_status = LoginResult.LOGIN_TIMEOUT
            last_message = (
                f"Timeout waiting for login result. Still at: {driver.current_url}"
            )

        diagnostics["visible_error"] = _collect_visible_login_errors(driver) or last_message
        print(f"  Login result: {last_status}")
        print(f"❌ Login failed ({last_status}): {last_message}")
        result = LoginResult(last_status, last_message, details=dict(diagnostics))
        _login_failure_alert(driver, result, diagnostics)
        return result

    except TimeoutException as e:
        print(f"❌ Selenium timeout during login: {e}")
        result = LoginResult(LoginResult.LOGIN_TIMEOUT, str(e))
        _login_failure_alert(driver, result, diagnostics)
        return result
    except Exception as e:
        print(f"❌ Login error: {e}")
        result = LoginResult(LoginResult.UNKNOWN_LOGIN_FAILURE, str(e))
        try:
            _login_failure_alert(driver, result, diagnostics)
        except Exception:
            send_error_notification(
                "LOGIN_FAILURE:UNKNOWN_LOGIN_FAILURE",
                e,
                traceback_text=traceback_mod.format_exc(),
            )
        return result


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

# Material icon ligature names that appear as plain text in Selenium .text
MATERIAL_ICON_NAMES = {
    "savings", "place", "insert_invitation", "schedule",
    "location_on", "attach_money", "event", "timer",
    "work", "business", "person", "star", "info",
    "person_pin_circle", "date_range", "watch_later",
    "home_work_filled", "home_work", "expand_more", "add",
}
_ICON_PREFIX_RE = re.compile(
    r'^(?:' + '|'.join(re.escape(n) for n in sorted(MATERIAL_ICON_NAMES, key=len, reverse=True)) + r')\s+',
    re.IGNORECASE,
)
_WORK_MODE_PHRASE_RE = re.compile(
    r'\b(?:hybrid|remote|on[- ]?site|onsite|occasionally|occasional|travel|primarily)\b',
    re.IGNORECASE,
)


def _strip_material_icons(text, join_with=" "):
    """Remove Material icon ligature names from scraped text."""
    if not text:
        return ""
    cleaned = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.lower() in MATERIAL_ICON_NAMES:
            continue
        s = _ICON_PREFIX_RE.sub("", s).strip()
        if s and s.lower() not in MATERIAL_ICON_NAMES:
            cleaned.append(s)
    return join_with.join(cleaned)


def _clean_location_geo(location):
    """Keep geography only; drop work-mode / travel phrases and icon noise."""
    if not location:
        return ""
    t = _strip_material_icons(location)
    parts = [p.strip() for p in re.split(r'[,;]', t) if p.strip()]
    geo = [p for p in parts if not _WORK_MODE_PHRASE_RE.search(p)]
    return ", ".join(geo) if geo else t


def _normalize_remote_type(value):
    """Canonical Hybrid / Remote / Onsite, or empty string."""
    if not value:
        return ""
    v = value.strip().lower().replace("_", " ")
    if "hybrid" in v:
        return "Hybrid"
    if re.search(r'\bon[- ]?site\b', v) or v == "onsite":
        return "Onsite"
    if "remote" in v:
        return "Remote"
    return ""


def _infer_remote_type(*text_parts):
    """Prefer explicit Hybrid, then Remote, then Onsite; demote occasional on-site."""
    block = "\n".join(p for p in text_parts if p)
    if not block:
        return ""

    # Explicit UI / labeled modes (Hybrid wins over incidental "on-site")
    if re.search(r'(?i)(?:^|\n)\s*hybrid\b', block) or re.search(r'(?i)\bhybrid\b\s*\(', block):
        return "Hybrid"
    if re.search(r'(?i)(?:^|\n)\s*remote\b', block):
        # primarily remote + occasional travel/on-site ≈ Hybrid
        if re.search(r'(?i)primarily\s+remote', block) and re.search(
            r'(?i)(?:occasional|travel|on[- ]?site)', block
        ):
            return "Hybrid"
        return "Remote"
    if re.search(r'(?i)(?:^|\n)\s*on[- ]?site\b', block):
        return "Onsite"

    # Fallback: phrase scan with Hybrid > primarily-remote hybrid cues > Remote > Onsite
    if re.search(r'(?i)\bhybrid\b', block):
        return "Hybrid"
    if re.search(r'(?i)primarily\s+remote', block):
        if re.search(r'(?i)(?:occasional|travel|on[- ]?site)', block):
            return "Hybrid"
        return "Remote"
    if re.search(r'(?i)\bremote\b', block) and not re.search(
        r'(?i)occasionally\s+on[- ]?site', block
    ):
        return "Remote"
    if re.search(r'(?i)\bon[- ]?site\b', block) and not re.search(
        r'(?i)occasional(?:ly)?\s+on[- ]?site', block
    ):
        return "Onsite"
    return ""


def _parse_project_location_block(body_text):
    """Parse BTG 'Project Location' section → (geo, remote_type, raw_block)."""
    m = re.search(
        r'(?:^|\n)\s*Project Location\s*\n'
        r'([\s\S]+?)'
        r'(?=\n(?:Timeline|date_range|Budget|savings|Apply Now|Deadline|'
        r'Requirements?|Level of Support|Not for you)|\Z)',
        body_text,
        re.IGNORECASE,
    )
    if not m:
        # Fallback: first person_pin_circle block (card/header style)
        m = re.search(
            r'(?:^|\n)\s*person_pin_circle\s*\n'
            r'([\s\S]+?)'
            r'(?=\n(?:Timeline|date_range|Budget|savings|Apply Now|Deadline|'
            r'Project Location|Requirements?|Level of Support|Not for you)|\Z)',
            body_text,
            re.IGNORECASE,
        )
    if not m:
        return "", "", ""
    raw = m.group(1).strip()
    skip_labels = {"project location", "location", "timeline", "budget"}
    lines = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.lower() in MATERIAL_ICON_NAMES or s.lower() in skip_labels:
            continue
        s = _ICON_PREFIX_RE.sub("", s).strip()
        if s and s.lower() not in MATERIAL_ICON_NAMES and s.lower() not in skip_labels:
            lines.append(s)
    if not lines:
        return "", "", raw

    remote_type = ""
    geo = ""
    for line in lines:
        mode = _normalize_remote_type(line)
        if mode and not remote_type:
            remote_type = mode
            continue
        if not geo and not _WORK_MODE_PHRASE_RE.search(line):
            geo = line
            continue
        if not geo and len(line) <= 80:
            # short region / country lines
            geo = _clean_location_geo(line) or line
    if not remote_type:
        remote_type = _infer_remote_type("\n".join(lines))
    return geo, remote_type, raw


def _extract_project_length(timeline, body_text, _sep):
    """Prefer duration from Timeline; avoid mid-sentence 'Duration' prose."""
    if timeline:
        paren = re.search(r'\(([^)]*\b(?:month|week|day)s?\b[^)]*)\)', timeline, re.IGNORECASE)
        if paren:
            return paren.group(1).strip()
        # whole timeline already encodes length
        if re.search(r'\b(?:month|week|day)s?\b', timeline, re.IGNORECASE):
            return timeline.strip()

    # Line-anchored labeled duration only (not "… Duration of 6–8 months) …")
    m = re.search(
        rf'(?:^|\n)\s*(?:Duration|Project Length|Expected Length)\b\s*:?{_sep}([^\n]{{2,60}})',
        body_text,
        re.IGNORECASE,
    )
    if m:
        val = m.group(1).strip()
        # Reject prose fragments sliced from description
        if re.match(r'(?i)(?:of|and)\b', val) or "primarily" in val.lower():
            return ""
        if re.search(r'\b(?:month|week|day|year)s?\b', val, re.IGNORECASE):
            return val[:60]
    return ""


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
                    t = _strip_material_icons(t)
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

        location   = _clean_location_geo(_first_text(card, LOCATION_SELECTORS, 80))
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

def _normalize_posted_date(time_str):
    """Normalize a posted-date string to MM/DD/YYYY, or '' if no date found."""
    if not time_str:
        return ""
    s = str(time_str).strip()
    m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', s)
    if m:
        return f"{int(m.group(1)):02d}/{int(m.group(2)):02d}/{m.group(3)}"
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        return f"{int(m.group(2)):02d}/{int(m.group(3)):02d}/{m.group(1)}"
    return ""


def make_dedupe_key(project_id, time_posted):
    """Dedupe key = project_id + posted date, so re-posts count as new."""
    if not project_id:
        return ""
    date = _normalize_posted_date(time_posted)
    return f"{project_id}|{date}" if date else str(project_id)


def _get_collection():
    """Return the MongoDB collection, reusing the client across calls."""
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(Config.MONGO_URI)
    return _mongo_client["office_monitor"]["projects"]

def init_db():
    """Ensure a unique index on 'dedupe_key' (project_id + posted date)."""
    coll = _get_collection()
    # Legacy unique index on project_id would block re-post inserts — drop it.
    try:
        coll.drop_index("idx_project_id_unique")
        print("  DB: dropped legacy unique index on project_id (re-posts now allowed)")
    except Exception:
        pass
    try:
        # sparse: existing docs without dedupe_key don't collide on the index
        coll.create_index("dedupe_key", unique=True, sparse=True, name="idx_dedupe_key_unique")
        coll.create_index("project_id", name="idx_project_id")
    except Exception:
        pass  # Indexes already exist — safe to ignore

def db_is_cold_start():
    """True if the collection has no documents (first ever run)."""
    return _get_collection().find_one({}, {"_id": 1}) is None

def get_seen_ids():
    """Return set of dedupe keys (project_id + posted date) already in DB."""
    try:
        docs = _get_collection().find(
            {}, {"project_id": 1, "time_posted": 1, "dedupe_key": 1, "_id": 0}
        )
        keys = set()
        for d in docs:
            # Older docs lack dedupe_key — rebuild it from stored fields
            key = d.get("dedupe_key") or make_dedupe_key(
                d.get("project_id"), d.get("time_posted")
            )
            if key:
                keys.add(key)
        return keys
    except Exception:
        return set()

def insert_project(project, emailed=True):
    """Upsert one project record keyed on dedupe_key (id + posted date)."""
    try:
        doc = {
            "dedupe_key":       make_dedupe_key(project.get("id"), project.get("time_posted")),
            "project_id":       project.get("id"),
            "title":            project.get("title"),
            "description":      project.get("description"),
            "location":         project.get("location"),
            "location_pref":    project.get("location_pref"),
            "remote_type":      project.get("remote_type"),
            "budget":           project.get("budget"),
            "duration":         project.get("duration"),
            "start_date":       project.get("start_date"),
            "timeline":         project.get("timeline"),
            "project_length":   project.get("project_length"),
            "engagement_type":  project.get("engagement_type"),
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
            {"dedupe_key": doc["dedupe_key"]},
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
                "dedupe_key":  make_dedupe_key(p.get("id"), p.get("time_posted")),
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
            ops.append(UpdateOne({"dedupe_key": doc["dedupe_key"]}, {"$setOnInsert": doc}, upsert=True))
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
    """Keep projects whose dedupe key (id + posted date) is unseen.
    No age filtering: every unseen project is emailed and stored.
    A re-post (same id, new posted date) counts as new again.
    """
    result = []
    for p in all_projects:
        if not p.get("id"):
            continue
        key = make_dedupe_key(p["id"], p.get("time_posted", ""))
        if key in seen_ids:
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

        # --- Click any "Read more" / "Show more" / expand buttons to reveal full text ---
        for btn_sel in [
            "//button[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'read more')]",
            "//button[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'show more')]",
            "//a[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'read more')]",
            "//a[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'show more')]",
            "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'see more')]",
            "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'view more')]",
            "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'view full')]",
        ]:
            try:
                btn = driver.find_element(By.XPATH, btn_sel)
                if btn.is_displayed():
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(2)
                    print(f"  Clicked expand button to reveal full description")
                    break
            except:
                continue

        # --- Also remove CSS truncation via JavaScript ---
        try:
            driver.execute_script("""
                document.querySelectorAll('[class*="description"], [class*="overview"], [class*="summary"]')
                    .forEach(function(el) {
                        el.style.maxHeight = 'none';
                        el.style.overflow = 'visible';
                        el.style.textOverflow = 'unset';
                        el.style.webkitLineClamp = 'unset';
                        el.style.display = 'block';
                    });
            """)
            time.sleep(1)
        except:
            pass

        # Try CSS selectors for full description (avoid broad page containers)
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

        # If CSS-extracted text still looks truncated (ends with "..."), try JS innerText
        if details.get("description", "").rstrip().endswith("..."):
            try:
                full_text = driver.execute_script("""
                    var candidates = document.querySelectorAll(
                        '[class*="description"], [class*="overview"], [class*="summary"]'
                    );
                    var best = '';
                    for (var i = 0; i < candidates.length; i++) {
                        var t = candidates[i].innerText || '';
                        if (t.length > best.length && t.length > 50) best = t;
                    }
                    return best.trim();
                """) or ""
                if full_text and len(full_text) > len(details.get("description", "")):
                    details["description"] = full_text
            except:
                pass

        body_text = driver.find_element(By.TAG_NAME, "body").text
        # Normalize non-breaking spaces and Windows line endings
        body_text = body_text.replace('\u00a0', ' ').replace('\r\n', '\n').replace('\r', '\n')

        # Primary extraction: BTG detail pages have no "Description" heading.
        # The description sits between the title/location header and "Apply Now".
        existing_desc = details.get("description", "")
        if not existing_desc or existing_desc.rstrip().endswith("...") or len(existing_desc) < 100:
            m = re.search(
                r'date_range\s+[^\n]+\n([\s\S]+?)(?=\n(?:add\n)?Apply Now|\nDeadline:|\nNot for you)',
                body_text, re.IGNORECASE
            )
            if m:
                txt = m.group(1).strip()
                if len(txt) > len(existing_desc):
                    details["description"] = txt

        # Fallback: extract description via labeled heading (other BTG layouts)
        existing_desc = details.get("description", "")
        if not existing_desc or existing_desc.rstrip().endswith("..."):
            m = re.search(
                r'(?:Description|Overview|Summary)\s*\n([\s\S]+?)(?=\n(?:Project Details|Budget|Location|Requirements|Qualifications|Apply|Start Date|Timeline|Deadline)|\Z)',
                body_text, re.IGNORECASE
            )
            if m:
                txt = m.group(1).strip()
                if len(txt) > len(existing_desc):
                    details["description"] = txt

        # Clean material icon text and nav cruft from description
        if details.get("description"):
            details["description"] = _strip_material_icons(
                details["description"], join_with="\n"
            )

        # Extract structured fields.
        # _SEP matches label→value separator in two formats:
        #   • same-line: "Timeline    6 months"  (spaces/tabs only)
        #   • next-line:  "Timeline\n6 months"   (newline, optional blank lines)
        _SEP = r'(?:[ \t]+|[ \t]*\n(?:[ \t]*\n)*[ \t]*)'
        patterns = {
            "start_date":       rf'(?:Start Date|Starts)\s*:?{_SEP}(\d{{2}}/\d{{2}}/\d{{4}}[^\n]{{0,40}})',
            "timeline":         rf'(?:Timeline|date_range){_SEP}(\d{{2}}/\d{{2}}/\d{{4}}[^\n]{{0,80}})',
            "engagement_type":  r'(?:Full time|Part time|Fractional)',
            "level_of_support": rf'Level of Support{_SEP}([^\n]{{2,60}})',
            "industry":         rf'(?:^|\n)(?:Industry|Desired Industry Background)\s*:?{_SEP}([^\n]{{2,100}})',
            "detail_budget":    rf'(?:Budget|savings){_SEP}(\$[^\n]{{2,80}})',
            "deadline":         rf'Deadline:?{_SEP}([^\n]{{2,30}})',
        }
        for field, pattern in patterns.items():
            m = re.search(pattern, body_text, re.IGNORECASE)
            if m:
                val = (m.group(1) if m.lastindex else m.group(0)).strip()
                if val:
                    details[field] = val

        # Project length: Timeline duration first; never mid-sentence "Duration …"
        proj_len = _extract_project_length(
            details.get("timeline", ""), body_text, _SEP
        )
        if proj_len:
            details["project_length"] = proj_len

        # Project Location block → clean geo + Hybrid/Remote/Onsite
        loc_geo, loc_mode, loc_block = _parse_project_location_block(body_text)
        if loc_geo:
            details["location"] = loc_geo
        remote_type = loc_mode or _infer_remote_type(
            loc_block,
            details.get("description", ""),
            body_text,
        )
        if remote_type:
            details["remote_type"] = remote_type
            details["location_pref"] = remote_type
            details["location_type"] = remote_type  # email display (backward compat)

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
        send_error_notification(
            "PROJECT_DETAIL_EXTRACTION_FAILURE",
            e,
            details=f"Failed while extracting project detail page.\nURL: {url}",
            traceback_text=traceback_mod.format_exc(),
        )
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
    location_type   = (
        project.get("remote_type")
        or project.get("location_pref")
        or project.get("location_type", "")
    )
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
        send_error_notification(
            "PROJECT_NOTIFICATION_FAILURE",
            e,
            details=(
                f"Failed to send project alert email for: "
                f"{project.get('title', 'Unknown')[:120]}\n"
                f"Project ID: {project.get('id', '')}\n"
                f"URL: {project.get('url', '')}"
            ),
            traceback_text=traceback_mod.format_exc(),
        )
        return False


# ============================
# DRIVER + SESSION SETUP
# ============================
def _find_binary(env_var, candidates):
    """Return the first existing path from env var or candidate list."""
    val = os.getenv(env_var, "")
    if val and os.path.exists(val):
        return val
    for path in candidates:
        if os.path.exists(path):
            return path
    for path in candidates:
        found = shutil.which(os.path.basename(path))
        if found:
            return found
    return ""


def _run_command_for_diagnostic(command):
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        output = (result.stdout or result.stderr or "").strip()
        return output or f"exit code {result.returncode}"
    except FileNotFoundError:
        return "not found"
    except Exception as e:
        return f"failed: {e}"


def print_browser_startup_diagnostics():
    """Print lightweight Chromium/ChromeDriver diagnostics for container startup."""
    chromium_path = shutil.which("chromium") or "not found"
    chromedriver_path = shutil.which("chromedriver") or "not found"
    print("  Browser startup diagnostics:")
    print(f"    which chromium: {chromium_path}")
    print(f"    chromium --version: {_run_command_for_diagnostic(['chromium', '--version'])}")
    print(f"    which chromedriver: {chromedriver_path}")
    print(f"    chromedriver --version: {_run_command_for_diagnostic(['chromedriver', '--version'])}")


def print_chromedriver_log():
    if not os.path.exists(CHROMEDRIVER_LOG_PATH):
        print(f"  ChromeDriver log not found: {CHROMEDRIVER_LOG_PATH}")
        return

    try:
        print(f"\n{'=' * 20} ChromeDriver log ({CHROMEDRIVER_LOG_PATH}) {'=' * 20}")
        with open(CHROMEDRIVER_LOG_PATH, "r", encoding="utf-8", errors="replace") as log_file:
            print(log_file.read().strip() or "(empty)")
        print(f"{'=' * 64}\n")
    except Exception as e:
        print(f"  Failed to read ChromeDriver log: {e}")


def prepare_chromedriver_log():
    try:
        open(CHROMEDRIVER_LOG_PATH, "w", encoding="utf-8").close()
    except Exception as e:
        print(f"  Could not initialize ChromeDriver log file: {e}")


def create_chromedriver_service(driver_path=""):
    service_kwargs = {
        "service_args": ["--verbose"],
        "log_output": CHROMEDRIVER_LOG_PATH,
    }
    if driver_path:
        return Service(driver_path, **service_kwargs)
    return Service(**service_kwargs)


def initialize_driver():
    print_browser_startup_diagnostics()
    prepare_chromedriver_log()

    options = Options()
    if Config.HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--remote-debugging-port=9222")
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
        service = create_chromedriver_service(system_path)
        print(f"  Chromedriver (system): {system_path}")
    else:
        # Fallback: webdriver-manager (downloads matching chromedriver)
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            from webdriver_manager.core.os_manager import ChromeType
            is_chromium = "chromium" in (chrome_bin or "").lower()
            mgr = ChromeDriverManager(chrome_type=ChromeType.CHROMIUM if is_chromium else ChromeType.GOOGLE)
            driver_path = mgr.install()
            service = create_chromedriver_service(driver_path)
            print(f"  Chromedriver (webdriver-manager): {driver_path}")
        except Exception as e:
            print(f"  Using default Service(): {e}")
            service = create_chromedriver_service()

    try:
        driver = webdriver.Chrome(service=service, options=options)
    except Exception as e:
        print_chromedriver_log()
        send_error_notification(
            "BROWSER_INIT_FAILURE",
            e,
            details="Failed to initialize Chromium / ChromeDriver.",
            traceback_text=traceback_mod.format_exc(),
            extra_rows=[
                ("Chromium", get_browser_versions().get("chromium")),
                ("ChromeDriver", get_browser_versions().get("chromedriver")),
            ],
        )
        raise

    driver.execute_cdp_cmd("Network.setUserAgentOverride", {
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    return driver


def setup_session(driver):
    """Try cookies first, fall back to login. Returns LoginResult."""
    if load_cookies(driver):
        driver.get(Config.PROJECTS_URL)
        time.sleep(5)
        # Check we're not kicked back to login
        url = (driver.current_url or "").lower()
        if "login" not in url and "sign" not in url:
            print("✅ Logged in via cookies")
            return LoginResult(LoginResult.SUCCESS, "Logged in via cookies")
        print("  Cookies expired — logging in fresh...")
        clear_stale_cookies()
        # Do not email merely because cookies expired if fresh login succeeds

    return perform_login(driver)


# ============================
# MAIN MONITORING LOOP
# ============================
def _alert_zero_projects(driver, streak):
    png_path, html_path = "", ""
    current_url = ""
    try:
        current_url = driver.current_url
    except Exception:
        pass
    try:
        png_path, html_path = save_login_failure_evidence(driver, prefix="btg_zero_projects")
    except Exception as e:
        print(f"  ⚠️ Zero-project evidence capture failed: {e}")
    send_error_notification(
        "ZERO_PROJECTS_EXTRACTED",
        f"No project cards extracted for {streak} consecutive scan(s)",
        details=(
            f"Projects page returned zero cards after retry.\n"
            f"URL: {current_url}\n"
            f"Screenshot: {png_path or 'n/a'}\n"
            f"HTML: {html_path or 'n/a'}\n\n"
            f"Visible text (truncated):\n{_safe_page_text(driver, 1500)}"
        ),
        attachments=[p for p in (png_path, html_path) if p],
        extra_rows=[
            ("Current URL", current_url),
            ("Consecutive failures", streak),
        ],
    )


def run_monitoring_loop(driver):
    """Poll BTG projects until KeyboardInterrupt or unrecoverable stop signal.
    Returns 'once' | 'auth_retry' | 'stop'.
    """
    global _monitor_check_count, _zero_project_streak

    if TEST_MODE:
        seen_ids = set()
        print("🧪 DB skipped — running in-memory only\n")
    else:
        try:
            cold_start = db_is_cold_start()
            init_db()
            seen_ids = get_seen_ids()
            print(f"📁 DB loaded — {len(seen_ids)} projects on record\n")
        except Exception as e:
            send_error_notification(
                "MONGODB_CONNECTION_FAILURE",
                e,
                details="Failed during MongoDB init / seen-id load.",
                traceback_text=traceback_mod.format_exc(),
            )
            raise

        if cold_start:
            print("⚙️  First run detected — seeding existing projects (no emails will be sent)...")
            seed_projects = scan_for_projects(driver)
            if seed_projects:
                bulk_insert_projects(seed_projects, emailed=False)
                print(
                    f"✅ Seeded {len(seed_projects)} existing projects. "
                    "Only NEW posts from now on will trigger emails.\n"
                )
                seen_ids = get_seen_ids()
            else:
                print("⚠️  Could not seed projects on first run — will try again next cycle.\n")

    last_keepalive = time.time()
    KEEPALIVE_INTERVAL = 1800  # refresh session every 30 minutes

    while True:
        try:
            _monitor_check_count += 1
            check_count = _monitor_check_count
            print(f"\n{'='*30}")
            print(f"🔄 Check #{check_count} — {datetime.now(PKT).strftime('%H:%M:%S')} PKT")
            print(f"{'='*30}")

            if time.time() - last_keepalive > KEEPALIVE_INTERVAL:
                save_cookies(driver)
                last_keepalive = time.time()
                print("  🔁 Session keep-alive: cookies refreshed")

            driver.get(Config.PROJECTS_URL)
            time.sleep(5)

            # If session expired, BTG silently redirects to /login — re-login immediately
            url = (driver.current_url or "").lower()
            if "login" in url or "sign" in url:
                print("  ⚠️ Session expired — clearing stale cookies and re-logging in...")
                clear_stale_cookies()
                login_result = perform_login(driver)
                if not login_result.ok:
                    print(
                        f"  ❌ Re-login failed ({login_result.status}). "
                        f"Authentication unavailable. Next login attempt in "
                        f"{Config.LOGIN_RETRY_INTERVAL} seconds."
                    )
                    return "auth_retry"
                driver.get(Config.PROJECTS_URL)
                time.sleep(5)

            all_projects = scan_for_projects(driver)

            # Retry once on empty extraction to avoid false alerts from slow loads
            if not all_projects:
                print("⚠️  No projects extracted — retrying page load once...")
                time.sleep(3)
                driver.get(Config.PROJECTS_URL)
                time.sleep(5)
                all_projects = scan_for_projects(driver)

            if not all_projects:
                _zero_project_streak += 1
                print(f"⚠️  No projects extracted this cycle (streak={_zero_project_streak})")
                if _zero_project_streak >= 2:
                    _alert_zero_projects(driver, _zero_project_streak)
                if ONCE_MODE:
                    return "once"
                time.sleep(Config.CHECK_INTERVAL)
                continue

            _zero_project_streak = 0
            new_projects = filter_new_projects(all_projects, seen_ids)

            if TEST_MODE and all_projects and not seen_ids:
                project = all_projects[0]
                print(f"🧪 TEST: Sending 1 test email → {project['title'][:60]}...")
                print("     Fetching full project details...")
                try:
                    details = fetch_project_details(driver, project['url'])
                    project.update(details)
                except Exception as e:
                    send_error_notification(
                        "PROJECT_DETAIL_EXTRACTION_FAILURE",
                        e,
                        details=f"URL: {project.get('url', '')}",
                        traceback_text=traceback_mod.format_exc(),
                    )
                send_notification(project)
                for p in all_projects:
                    seen_ids.add(make_dedupe_key(p["id"], p.get("time_posted", "")))
            elif new_projects:
                print(f"🎯 Found {len(new_projects)} NEW project(s)!")
                for project in new_projects:
                    print(f"  → {project['title'][:60]}...")
                    print("     Fetching full project details...")
                    try:
                        details = fetch_project_details(driver, project['url'])
                        project.update(details)
                    except Exception as e:
                        print(f"  ⚠️ Detail fetch exception: {e}")
                        send_error_notification(
                            "PROJECT_DETAIL_EXTRACTION_FAILURE",
                            e,
                            details=(
                                f"Title: {project.get('title', '')[:120]}\n"
                                f"URL: {project.get('url', '')}"
                            ),
                            traceback_text=traceback_mod.format_exc(),
                        )
                    emailed = send_notification(project)
                    if not TEST_MODE:
                        insert_project(project, emailed=emailed)
                    seen_ids.add(
                        make_dedupe_key(project['id'], project.get('time_posted', ''))
                    )
            else:
                print("⏳ No new projects this cycle")

            print(f"📊 Stats: {len(all_projects)} visible, {len(seen_ids)} in DB")

            if ONCE_MODE:
                print("\n✅ Once mode complete. Exiting.")
                return "once"

            print(f"\n⏳ Next check in {Config.CHECK_INTERVAL}s...")
            time.sleep(Config.CHECK_INTERVAL)

        except KeyboardInterrupt:
            raise
        except Exception as loop_err:
            print(
                f"⚠️ Check failed: {loop_err} — "
                f"reinitializing driver in {Config.LOGIN_RETRY_INTERVAL}s..."
            )
            send_error_notification(
                "MONITORING_CYCLE_EXCEPTION",
                loop_err,
                details="Unhandled exception inside the project monitoring cycle.",
                traceback_text=traceback_mod.format_exc(),
            )
            _safe_quit(driver)
            time.sleep(Config.LOGIN_RETRY_INTERVAL)
            driver = initialize_driver()
            login_result = setup_session(driver)
            if not login_result.ok:
                print(
                    f"Authentication unavailable. Next login attempt in "
                    f"{Config.LOGIN_RETRY_INTERVAL} seconds."
                )
                _safe_quit(driver)
                return "auth_retry"


def main():
    print("=" * 50)
    print("🚀 BTG Project Monitor")
    if DEBUG_MODE:
        print("   (DEBUG MODE ON — page structure will be printed)")
    print("=" * 50)
    print(f"  Account  : {Config.BTG_EMAIL}")
    print(f"  Interval : {Config.CHECK_INTERVAL}s")
    print(f"  Login retry: {Config.LOGIN_RETRY_INTERVAL}s")
    print("  Max age  : disabled (all unseen projects are saved & emailed)")
    print(f"  Recipients: {', '.join(Config.RECIPIENT_EMAILS)}")
    if Config.ERROR_RECIPIENTS:
        print(f"  Error alerts: {', '.join(Config.ERROR_RECIPIENTS)}")
    else:
        print("  Error alerts: NOT CONFIGURED (set error_recipent)")
    print(f"  Error cooldown: {Config.ERROR_EMAIL_COOLDOWN_MINUTES} minutes")
    print()

    if TEST_MODE:
        Config.RECIPIENT_EMAILS = ["muhammadammar7747@gmail.com"]
        print("🧪 TEST MODE — MongoDB skipped, 1 test email → muhammadammar7747@gmail.com\n")

    # Single supervisory loop — auth failures wait LOGIN_RETRY_INTERVAL once (no double sleep)
    while True:
        driver = None
        try:
            driver = initialize_driver()
            login_result = setup_session(driver)
            if not login_result.ok:
                print(f"❌ Failed to establish BTG session ({login_result.status})")
                print(
                    f"Authentication unavailable. Next login attempt in "
                    f"{Config.LOGIN_RETRY_INTERVAL} seconds."
                )
                _safe_quit(driver)
                driver = None
                time.sleep(Config.LOGIN_RETRY_INTERVAL)
                continue

            driver.get(Config.PROJECTS_URL)
            time.sleep(4)

            outcome = run_monitoring_loop(driver)
            _safe_quit(driver)
            driver = None

            if outcome == "once":
                print("✅ BTG Monitor stopped")
                return
            if outcome == "auth_retry":
                print(
                    f"Authentication unavailable. Next login attempt in "
                    f"{Config.LOGIN_RETRY_INTERVAL} seconds."
                )
                time.sleep(Config.LOGIN_RETRY_INTERVAL)
                continue

            # Unexpected monitoring exit — controlled retry, not "unexpected crash"
            print(
                f"Monitoring loop ended ({outcome}). "
                f"Retrying in {Config.LOGIN_RETRY_INTERVAL} seconds..."
            )
            time.sleep(Config.LOGIN_RETRY_INTERVAL)

        except KeyboardInterrupt:
            _safe_quit(driver)
            raise
        except Exception as e:
            print(f"\n❌ Unexpected error: {e}")
            traceback_mod.print_exc()
            send_error_notification(
                "FATAL_MONITOR_EXCEPTION",
                e,
                details="Unexpected exception in main supervisory loop.",
                traceback_text=traceback_mod.format_exc(),
            )
            _safe_quit(driver)
            print(
                f"Retrying after unexpected error in {Config.LOGIN_RETRY_INTERVAL} seconds..."
            )
            time.sleep(Config.LOGIN_RETRY_INTERVAL)


if __name__ == "__main__":
    if TEST_ERROR_EMAIL_MODE:
        ok = run_test_error_email()
        sys.exit(0 if ok else 1)

    try:
        main()
    except KeyboardInterrupt:
        print("\n⏹️  Stopped by user")
    except Exception as fatal:
        print(f"💥 Fatal crash: {fatal}")
        send_error_notification(
            "FATAL_OUTER_CRASH",
            fatal,
            details="Unhandled exception escaped main().",
            traceback_text=traceback_mod.format_exc(),
            force=True,
        )
        sys.exit(1)
