import time
import smtplib
import json
import os
import re
import hashlib
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import traceback as traceback_mod
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

# Load .env file from this script's directory
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

PKT = timezone(timedelta(hours=5))  # Pakistan Standard Time (UTC+5)

# ============================
# CONFIGURATION
# ============================
def _env_bool(name, default="false"):
    return os.getenv(name, default).lower() in ("1", "true", "yes", "on")


def _resolve_evidence_dir():
    volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if volume:
        path = os.path.join(volume, "evidence")
    else:
        path = os.getenv("EVIDENCE_DIR", "/tmp/btg-evidence")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        path = tempfile.gettempdir()
        os.makedirs(path, exist_ok=True)
    return path


def _resolve_cookie_file():
    explicit = os.getenv("COOKIE_FILE") or os.getenv("BTG_COOKIES_FILE")
    if explicit:
        return explicit
    volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if volume:
        return os.path.join(volume, "btg_cookies.json")
    return "/tmp/btg_cookies.json"


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
    REPOST_MIN_DAYS = int(os.getenv("REPOST_MIN_DAYS", "3"))
    HEADLESS     = _env_bool("HEADLESS", "False")
    BTG_LOGIN_DIAGNOSTIC_MODE = _env_bool("BTG_LOGIN_DIAGNOSTIC_MODE", "false")
    BTG_CLEAR_SESSION_ON_START = _env_bool("BTG_CLEAR_SESSION_ON_START", "false")
    BTG_CAPTURE_NETWORK_LOGS = _env_bool("BTG_CAPTURE_NETWORK_LOGS", "false")
    BTG_PAUSE_AFTER_LOGIN_FAILURE = _env_bool("BTG_PAUSE_AFTER_LOGIN_FAILURE", "false")

    # Preflight / Railway
    BTG_PREFLIGHT_ENABLED = _env_bool(
        "BTG_PREFLIGHT_ENABLED",
        "true" if (
            os.getenv("RAILWAY_ENVIRONMENT")
            or os.getenv("RAILWAY_SERVICE_ID")
            or os.getenv("RAILWAY_PROJECT_ID")
        ) else "false",
    )
    BTG_PREFLIGHT_URL = os.getenv(
        "BTG_PREFLIGHT_URL",
        "https://api.businesstalentgroup.com/auth/sign_in",
    )
    BTG_PREFLIGHT_TIMEOUT = int(os.getenv("BTG_PREFLIGHT_TIMEOUT", "30"))
    BTG_PREFLIGHT_FAILURE_RETRY_SECONDS = int(
        os.getenv("BTG_PREFLIGHT_FAILURE_RETRY_SECONDS", "1800")
    )
    CHROME_BIN = os.getenv("CHROME_BIN", "/usr/bin/chromium")
    CHROMEDRIVER_PATH = os.getenv("CHROMEDRIVER_PATH", "/usr/bin/chromedriver")
    EVIDENCE_DIR = _resolve_evidence_dir()
    EVIDENCE_RETENTION_HOURS = int(os.getenv("EVIDENCE_RETENTION_HOURS", "24"))
    COOKIES_FILE = _resolve_cookie_file()
    BTG_WORKER_LOCK_ENABLED = _env_bool("BTG_WORKER_LOCK_ENABLED", "true")
    BTG_WORKER_LOCK_TTL_SECONDS = int(os.getenv("BTG_WORKER_LOCK_TTL_SECONDS", "180"))
    HEALTH_PORT = int(os.getenv("PORT", "8080"))
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
TEST_BTG_LOGIN_MODE = "--test-btg-login" in sys.argv
TEST_BTG_PREFLIGHT_MODE = "--test-btg-preflight" in sys.argv
PRINT_RUNTIME_DIAGNOSTICS_MODE = "--print-runtime-diagnostics" in sys.argv
CHROMEDRIVER_LOG_PATH = "/tmp/chromedriver.log"

# Runtime state for operational alerts (never stores secrets)
_error_email_last_sent = {}
_sending_error_email = False
_monitor_check_count = 0
_zero_project_streak = 0
_browser_versions_cache = None
_chrome_profile_dir = None
shutdown_event = threading.Event()
_health_server = None
_worker_lock_owner = None

_monitor_state = {
    "status": "ok",
    "service": "btg-project-monitor",
    "process_alive": True,
    "monitor_state": "starting",
    "last_successful_scan": None,
    "last_login_result": None,
    "last_preflight": None,
    "timestamp": None,
}
_monitor_state_lock = threading.Lock()


def set_monitor_state(state, **extra):
    with _monitor_state_lock:
        _monitor_state["monitor_state"] = state
        _monitor_state["timestamp"] = datetime.now(PKT).isoformat()
        for k, v in extra.items():
            _monitor_state[k] = v


def get_monitor_state_snapshot():
    with _monitor_state_lock:
        snap = dict(_monitor_state)
    snap["timestamp"] = datetime.now(PKT).isoformat()
    return snap


def interruptible_sleep(seconds):
    """Sleep that wakes early on SIGTERM/SIGINT via shutdown_event."""
    return shutdown_event.wait(timeout=max(0, float(seconds)))


def is_railway_environment():
    return bool(
        os.getenv("RAILWAY_ENVIRONMENT")
        or os.getenv("RAILWAY_SERVICE_ID")
        or os.getenv("RAILWAY_PROJECT_ID")
    )


def railway_metadata():
    return {
        "environment": os.getenv("RAILWAY_ENVIRONMENT", ""),
        "service": os.getenv("RAILWAY_SERVICE_NAME", "") or os.getenv("RAILWAY_SERVICE_ID", ""),
        "region": os.getenv("RAILWAY_REGION", "") or os.getenv("RAILWAY_REPLICA_REGION", ""),
        "deployment_id": os.getenv("RAILWAY_DEPLOYMENT_ID", ""),
        "project_id": os.getenv("RAILWAY_PROJECT_ID", ""),
    }


def log_event(severity, event, classification="", **fields):
    ts = datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S PKT")
    meta = railway_metadata()
    parts = [
        f"ts={ts}",
        f"severity={severity}",
        f"event={event}",
    ]
    if classification:
        parts.append(f"classification={classification}")
    if meta.get("deployment_id"):
        parts.append(f"railway_deployment={meta['deployment_id']}")
    for k, v in fields.items():
        if v is None or v == "":
            continue
        parts.append(f"{k}={v}")
    print(" | ".join(parts), flush=True)


def worker_owner_id():
    return (
        os.getenv("RAILWAY_DEPLOYMENT_ID")
        or os.getenv("RAILWAY_REPLICA_ID")
        or socket.gethostname()
    )

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
    INVALID_CREDENTIALS_RESPONSE = "INVALID_CREDENTIALS_RESPONSE"
    CORS_PREFLIGHT_FAILED = "CORS_PREFLIGHT_FAILED"
    BTG_EDGE_403 = "BTG_EDGE_403"
    BTG_RATE_LIMITED = "BTG_RATE_LIMITED"
    HTTP_401 = "HTTP_401"
    HTTP_403 = "HTTP_403"
    HTTP_429 = "HTTP_429"
    HTTP_5XX = "HTTP_5XX"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    MFA_REQUIRED = "MFA_REQUIRED"
    ACCESS_DENIED = "ACCESS_DENIED"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    FORM_VALUE_MISMATCH = "FORM_VALUE_MISMATCH"
    LOGIN_TIMEOUT = "LOGIN_TIMEOUT"
    LOGIN_PAGE_CHANGED = "LOGIN_PAGE_CHANGED"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    UNKNOWN_LOGIN_FAILURE = "UNKNOWN_LOGIN_FAILURE"

    AUTH_BLOCKERS = {
        INVALID_CREDENTIALS,
        INVALID_CREDENTIALS_RESPONSE,
        CORS_PREFLIGHT_FAILED,
        BTG_EDGE_403,
        BTG_RATE_LIMITED,
        HTTP_401,
        HTTP_403,
        HTTP_429,
        HTTP_5XX,
        CAPTCHA_REQUIRED,
        MFA_REQUIRED,
        ACCESS_DENIED,
        ACCOUNT_LOCKED,
        FORM_VALUE_MISMATCH,
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


def safe_fingerprint(value):
    """SHA-256 prefix (12 hex chars) for safe credential comparison. Never logs the value."""
    if value is None:
        return "EMPTY"
    if value == "":
        return "EMPTY"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def credential_diagnostics(email_value, configured_password, typed_password):
    """Safe credential diagnostics — lengths and fingerprints only, never plaintext."""
    email_value = "" if email_value is None else str(email_value)
    configured_password = "" if configured_password is None else str(configured_password)
    typed_password = "" if typed_password is None else str(typed_password)

    cfg_fp = safe_fingerprint(configured_password)
    typed_fp = safe_fingerprint(typed_password)
    passwords_match = configured_password == typed_password
    email_ws = email_value != email_value.strip()
    password_ws = configured_password != configured_password.strip()
    email_empty = email_value == ""
    password_empty = configured_password == ""

    print(f"  Configured BTG email: {email_value!r}")
    print(f"  Configured password length: {len(configured_password)}")
    print(f"  Typed password length: {len(typed_password)}")
    print(f"  Configured password fingerprint: {cfg_fp}")
    print(f"  Typed password fingerprint: {typed_fp}")
    print(f"  Configured and typed passwords match: {passwords_match}")
    print(f"  Email surrounding whitespace: {email_ws}")
    print(f"  Password surrounding whitespace: {password_ws}")
    if email_empty or password_empty:
        print(f"  Configured email empty: {email_empty}")
        print(f"  Configured password empty: {password_empty}")

    return {
        "configured_email_repr": repr(email_value),
        "configured_password_length": len(configured_password),
        "typed_password_length": len(typed_password),
        "configured_password_fingerprint": cfg_fp,
        "typed_password_fingerprint": typed_fp,
        "password_values_match": passwords_match,
        "email_surrounding_whitespace": email_ws,
        "password_surrounding_whitespace": password_ws,
        "email_empty": email_empty,
        "password_empty": password_empty,
    }


def get_native_browser_info(driver):
    """Read native navigator identity (no cookies/storage/secrets)."""
    try:
        info = driver.execute_script(
            """
            return {
                userAgent: navigator.userAgent || '',
                platform: navigator.platform || '',
                language: navigator.language || '',
                languages: navigator.languages || [],
                webdriver: !!navigator.webdriver
            };
            """
        ) or {}
        return {
            "userAgent": info.get("userAgent", ""),
            "platform": info.get("platform", ""),
            "language": info.get("language", ""),
            "languages": list(info.get("languages") or []),
            "webdriver": bool(info.get("webdriver")),
        }
    except Exception as e:
        print(f"  ⚠️ Could not read native browser info: {e}")
        return {
            "userAgent": "",
            "platform": "",
            "language": "",
            "languages": [],
            "webdriver": None,
        }


def redact_sensitive_text(text):
    """Redact credentials, JWTs, bearer tokens, and cookie-like values from log text."""
    if text is None:
        return ""
    out = str(text)
    email = Config.BTG_EMAIL or ""
    password = Config.BTG_PASSWORD or ""
    if email:
        out = out.replace(email, "[REDACTED_EMAIL]")
    if password:
        out = out.replace(password, "[REDACTED_PASSWORD]")
    out = re.sub(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*", "Bearer [REDACTED_TOKEN]", out)
    out = re.sub(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
        "[REDACTED_JWT]",
        out,
    )
    out = re.sub(
        r"(?i)(cookie|set-cookie|authorization)\s*[:=]\s*[^;\s]+",
        r"\1=[REDACTED]",
        out,
    )
    return out


def empty_session_cleanup_status():
    return {
        "mongo_cookie_deleted": False,
        "local_cookie_file_deleted": False,
        "selenium_cookies_cleared": False,
        "cdp_cookies_cleared": False,
        "browser_cache_cleared": False,
        "local_storage_cleared": False,
        "session_storage_cleared": False,
    }


# ============================
# HEALTH SERVER / PREFLIGHT / LOCK
# ============================
class _QuietHealthHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003
        return

    def do_GET(self):
        if self.path.split("?")[0] != "/health":
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"not_found"}')
            return
        payload = get_monitor_state_snapshot()
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_health_server():
    global _health_server
    port = Config.HEALTH_PORT
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), _QuietHealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True, name="health-server")
        thread.start()
        _health_server = server
        log_event("INFO", "health_server_started", port=port)
        return server
    except Exception as e:
        log_event("ERROR", "health_server_failed", message=str(e))
        return None


def stop_health_server():
    global _health_server
    if _health_server is None:
        return
    try:
        _health_server.shutdown()
    except Exception:
        pass
    _health_server = None


def request_shutdown(signum=None, frame=None):
    log_event("INFO", "shutdown_requested", signal=signum)
    set_monitor_state("shutting_down")
    shutdown_event.set()


def install_signal_handlers():
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, request_shutdown)
        except Exception:
            pass


def validate_configuration():
    """Return (ok, missing_names). Never prints secret values."""
    required = {
        "BTG_EMAIL": Config.BTG_EMAIL,
        "BTG_PASSWORD": Config.BTG_PASSWORD,
        "MONGO_URI": Config.MONGO_URI,
        "SMTP_SERVER": Config.SMTP_SERVER,
        "SMTP_PORT": str(Config.SMTP_PORT) if Config.SMTP_PORT else "",
        "SENDER_EMAIL": Config.SENDER_EMAIL,
        "SENDER_PASSWORD": Config.SENDER_PASSWORD,
        "RECIPIENT_EMAILS": ",".join(Config.RECIPIENT_EMAILS),
        "error_recipent": ",".join(Config.ERROR_RECIPIENTS),
    }
    missing = [name for name, val in required.items() if not (val or "").strip()]
    return (len(missing) == 0), missing


def cleanup_old_evidence():
    """Delete evidence files older than EVIDENCE_RETENTION_HOURS."""
    root = Config.EVIDENCE_DIR
    try:
        cutoff = time.time() - max(Config.EVIDENCE_RETENTION_HOURS, 1) * 3600
        removed = 0
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            if not name.startswith("btg_"):
                continue
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except Exception:
                pass
        if removed:
            log_event("INFO", "evidence_cleanup", removed=removed)
    except Exception as e:
        log_event("WARN", "evidence_cleanup_failed", message=str(e))


def check_btg_auth_preflight():
    """Safe OPTIONS preflight to BTG auth API. Never sends credentials."""
    url = Config.BTG_PREFLIGHT_URL
    started = time.time()
    result = {
        "ok": False,
        "classification": "BTG_PREFLIGHT_UNKNOWN",
        "status": None,
        "server": None,
        "allow_origin": None,
        "location": None,
        "elapsed_ms": 0,
        "message": "",
    }
    if requests is None:
        result["classification"] = "BTG_NETWORK_ERROR"
        result["message"] = "requests package is not installed"
        return result

    headers = {
        "Origin": "https://talent.businesstalentgroup.com",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
        "User-Agent": "BTG-Monitor-Connectivity-Check/1.0",
    }
    try:
        resp = requests.options(
            url,
            headers=headers,
            timeout=Config.BTG_PREFLIGHT_TIMEOUT,
            allow_redirects=False,
        )
        result["status"] = resp.status_code
        result["server"] = resp.headers.get("server") or resp.headers.get("Server")
        result["allow_origin"] = (
            resp.headers.get("access-control-allow-origin")
            or resp.headers.get("Access-Control-Allow-Origin")
        )
        result["location"] = resp.headers.get("location") or resp.headers.get("Location")
        result["elapsed_ms"] = int((time.time() - started) * 1000)

        status = resp.status_code
        if status in (200, 204):
            ao = (result["allow_origin"] or "").strip()
            if ao in ("*", "https://talent.businesstalentgroup.com"):
                result["ok"] = True
                result["classification"] = "BTG_PREFLIGHT_OK"
                result["message"] = "Preflight succeeded with CORS allow-origin"
            else:
                result["classification"] = "BTG_CORS_HEADERS_MISSING"
                result["message"] = "Preflight returned success status but Access-Control-Allow-Origin missing/unexpected"
        elif status == 403:
            result["classification"] = "BTG_EDGE_403"
            result["message"] = (
                "BTG's edge layer returned HTTP 403 to the authentication preflight "
                "request. Credentials were not sent and Selenium login should be skipped."
            )
        elif status == 429:
            result["classification"] = "BTG_RATE_LIMITED"
            result["message"] = "BTG rate-limited the authentication preflight (HTTP 429)"
        elif 500 <= status <= 599:
            result["classification"] = "BTG_SERVER_ERROR"
            result["message"] = f"BTG auth API returned HTTP {status}"
        elif status in (301, 302, 303, 307, 308):
            result["classification"] = "BTG_PREFLIGHT_REDIRECT"
            result["message"] = f"Preflight redirected to {result['location'] or '(unknown)'}"
        else:
            result["classification"] = "BTG_PREFLIGHT_UNKNOWN"
            result["message"] = f"Unexpected preflight status {status}"
    except Exception as e:
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        result["classification"] = "BTG_NETWORK_ERROR"
        result["message"] = f"Preflight network error: {e}"

    log_event(
        "INFO" if result["ok"] else "WARN",
        "btg_preflight",
        classification=result["classification"],
        status=result["status"],
        server=result["server"],
        elapsed_ms=result["elapsed_ms"],
    )
    return result


def acquire_worker_lock():
    """MongoDB lease so only one replica scans. Returns True if this process owns the lock."""
    global _worker_lock_owner
    if not Config.BTG_WORKER_LOCK_ENABLED:
        return True
    owner = worker_owner_id()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=Config.BTG_WORKER_LOCK_TTL_SECONDS)
    coll = _get_session_collection().database["worker_locks"]
    try:
        doc = coll.find_one({"_id": "btg_monitor_worker_lock"})
        if doc:
            exp = doc.get("expires_at")
            if isinstance(exp, datetime) and exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp and exp > now and doc.get("owner") != owner:
                log_event(
                    "WARN",
                    "worker_lock_held",
                    owner=doc.get("owner"),
                    expires_at=str(exp),
                )
                return False
        coll.update_one(
            {"_id": "btg_monitor_worker_lock"},
            {"$set": {
                "owner": owner,
                "expires_at": expires,
                "heartbeat_at": now,
            }},
            upsert=True,
        )
        _worker_lock_owner = owner
        return True
    except Exception as e:
        log_event("ERROR", "worker_lock_acquire_failed", message=str(e))
        # Fail open for availability if lock store is down
        return True


def renew_worker_lock():
    if not Config.BTG_WORKER_LOCK_ENABLED or not _worker_lock_owner:
        return
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=Config.BTG_WORKER_LOCK_TTL_SECONDS)
    try:
        coll = _get_session_collection().database["worker_locks"]
        coll.update_one(
            {"_id": "btg_monitor_worker_lock", "owner": _worker_lock_owner},
            {"$set": {"expires_at": expires, "heartbeat_at": now}},
        )
    except Exception as e:
        log_event("WARN", "worker_lock_renew_failed", message=str(e))


def release_worker_lock():
    global _worker_lock_owner
    if not Config.BTG_WORKER_LOCK_ENABLED or not _worker_lock_owner:
        return
    try:
        coll = _get_session_collection().database["worker_locks"]
        coll.delete_one({"_id": "btg_monitor_worker_lock", "owner": _worker_lock_owner})
    except Exception as e:
        log_event("WARN", "worker_lock_release_failed", message=str(e))
    _worker_lock_owner = None


def alert_preflight_failure(preflight):
    classification = preflight.get("classification")
    if classification == "BTG_EDGE_403":
        message = (
            "BTG's edge layer returned HTTP 403 to the authentication preflight "
            "request from the Railway deployment. Credentials were not sent and "
            "Selenium login was skipped."
        )
    else:
        message = preflight.get("message") or classification
    set_monitor_state(
        "degraded",
        last_preflight=preflight,
        status="degraded",
    )
    send_error_notification(
        f"BTG_PREFLIGHT:{classification}",
        message,
        details=(
            "BTG authentication was not attempted because the safe OPTIONS preflight failed.\n"
            "Credentials were not sent.\n\n"
            f"classification={classification}\n"
            f"status={preflight.get('status')}\n"
            f"server={preflight.get('server')}\n"
            f"allow_origin={preflight.get('allow_origin')}\n"
            f"location={preflight.get('location')}\n"
            f"elapsed_ms={preflight.get('elapsed_ms')}\n"
        ),
    )


def print_runtime_diagnostics():
    versions = get_browser_versions()
    meta = railway_metadata()
    ok, missing = validate_configuration()
    print("=" * 60)
    print("BTG runtime diagnostics")
    print("=" * 60)
    print(f"Python version: {sys.version.split()[0]}")
    print(f"Chromium path: {Config.CHROME_BIN}")
    print(f"Chromium version: {versions.get('chromium')}")
    print(f"ChromeDriver path: {Config.CHROMEDRIVER_PATH}")
    print(f"ChromeDriver version: {versions.get('chromedriver')}")
    print(f"Railway detected: {is_railway_environment()}")
    print(f"Railway environment: {meta.get('environment') or '(none)'}")
    print(f"Railway service: {meta.get('service') or '(none)'}")
    print(f"Railway region: {meta.get('region') or '(none)'}")
    print(f"Railway deployment: {meta.get('deployment_id') or '(none)'}")
    print(f"PORT: {Config.HEALTH_PORT}")
    print(f"MongoDB configured: {'yes' if Config.MONGO_URI else 'no'}")
    print(f"SMTP configured: {'yes' if Config.SENDER_EMAIL and Config.SENDER_PASSWORD else 'no'}")
    print(f"BTG email configured: {'yes' if Config.BTG_EMAIL else 'no'}")
    print(f"Error recipients configured: {'yes' if Config.ERROR_RECIPIENTS else 'no'}")
    print(f"Headless mode: {Config.HEADLESS and not Config.BTG_LOGIN_DIAGNOSTIC_MODE}")
    print(f"Preflight enabled: {Config.BTG_PREFLIGHT_ENABLED}")
    print(f"Evidence dir: {Config.EVIDENCE_DIR}")
    print(f"Cookie file: {Config.COOKIES_FILE}")
    print(f"Config valid: {ok}")
    if missing:
        print(f"Missing variables: {', '.join(missing)}")
    print("=" * 60)


def run_test_btg_preflight():
    print("=" * 60)
    print("BTG auth preflight test (--test-btg-preflight)")
    print("=" * 60)
    result = check_btg_auth_preflight()
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


def _evidence_dir():
    path = Config.EVIDENCE_DIR
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except Exception:
        fallback = tempfile.gettempdir()
        os.makedirs(fallback, exist_ok=True)
        return fallback


def _cookie_file_path():
    path = Config.COOKIES_FILE
    if os.path.isabs(path):
        return path
    return os.path.join(os.path.dirname(__file__), path)


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
    meta = railway_metadata()
    snap = get_monitor_state_snapshot()
    preflight = snap.get("last_preflight") or {}
    rows = [
        ("Context", _html_esc(context)),
        ("Exception", f"{_html_esc(err_type)}: {err_msg}"),
        ("Timestamp", _html_esc(now)),
        ("Hostname", _html_esc(hostname)),
        ("Railway environment", _html_esc(meta.get("environment") or "(none)")),
        ("Railway service", _html_esc(meta.get("service") or "(none)")),
        ("Railway region", _html_esc(meta.get("region") or "(none)")),
        ("Railway deployment ID", _html_esc(meta.get("deployment_id") or "(none)")),
        ("Check #", str(_monitor_check_count or "—")),
        ("Monitor state", _html_esc(str(snap.get("monitor_state") or ""))),
        ("Headless", str(Config.HEADLESS)),
        ("Native Chromium version", _html_esc(versions.get("chromium", "unknown"))),
        ("ChromeDriver version", _html_esc(versions.get("chromedriver", "unknown"))),
        ("Preflight classification", _html_esc(str(preflight.get("classification") or "(n/a)"))),
        ("Preflight HTTP status", _html_esc(str(preflight.get("status") if preflight else "(n/a)"))),
        ("Preflight server header", _html_esc(str(preflight.get("server") or "(n/a)"))),
        ("Preflight allow-origin", _html_esc(str(preflight.get("allow_origin") or "(n/a)"))),
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


def save_login_failure_evidence(
    driver,
    prefix="btg_login_failure",
    diagnostics=None,
    network_responses=None,
    console_entries=None,
):
    """Save screenshot + HTML + safe JSON (+ optional network/console). Returns paths dict."""
    ts = datetime.now(PKT).strftime("%Y%m%d_%H%M%S")
    base = os.path.join(_evidence_dir(), f"{prefix}_{ts}")
    paths = {
        "png": f"{base}.png",
        "html": f"{base}.html",
        "json": f"{base}.json",
        "network": f"{base}_network.json",
        "console": f"{base}_console.json",
    }
    try:
        driver.save_screenshot(paths["png"])
        print(f"  Saved login failure screenshot: {paths['png']}")
    except Exception as e:
        print(f"  ⚠️ Screenshot failed: {e}")
        paths["png"] = ""
    try:
        with open(paths["html"], "w", encoding="utf-8", errors="replace") as fh:
            fh.write(driver.page_source or "")
        print(f"  Saved login failure HTML: {paths['html']}")
    except Exception as e:
        print(f"  ⚠️ HTML capture failed: {e}")
        paths["html"] = ""
    try:
        with open(paths["json"], "w", encoding="utf-8") as fh:
            json.dump(diagnostics or {}, fh, indent=2, default=str)
        print(f"  Saved login failure JSON: {paths['json']}")
    except Exception as e:
        print(f"  ⚠️ JSON diagnostics failed: {e}")
        paths["json"] = ""
    if network_responses is not None:
        try:
            with open(paths["network"], "w", encoding="utf-8") as fh:
                json.dump(network_responses, fh, indent=2, default=str)
            print(f"  Saved network diagnostics: {paths['network']}")
        except Exception as e:
            print(f"  ⚠️ Network JSON failed: {e}")
            paths["network"] = ""
    else:
        paths["network"] = ""
    if console_entries is not None:
        try:
            with open(paths["console"], "w", encoding="utf-8") as fh:
                json.dump(console_entries, fh, indent=2, default=str)
            print(f"  Saved console diagnostics: {paths['console']}")
        except Exception as e:
            print(f"  ⚠️ Console JSON failed: {e}")
            paths["console"] = ""
    else:
        paths["console"] = ""
    return paths


def _dispatch_angular_events(driver, element):
    driver.execute_script(
        """
        const field = arguments[0];
        field.dispatchEvent(new Event('input', { bubbles: true }));
        field.dispatchEvent(new Event('change', { bubbles: true }));
        field.dispatchEvent(new Event('blur', { bubbles: true }));
        """,
        element,
    )


def _fill_input_field(driver, element, value, label, is_password=False):
    """Click, clear via Ctrl+A/Backspace, type, fire Angular events, verify exact value.
    Returns (ok: bool, actual_value: str).
    """
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
    actual = element.get_attribute("value")
    if actual is None:
        actual = ""
    # Exact match required — do not strip (whitespace may be intentional for passwords)
    if actual != value:
        if is_password:
            print(
                f"  ⚠️ Password value mismatch after fill "
                f"(configured length {len(value)}, typed length {len(actual)})"
            )
        else:
            print(f"  ⚠️ {label} value mismatch after fill")
        return False, actual
    if is_password:
        print(f"  Password field populated and verified: {len(actual)} characters.")
    else:
        print(f"  {label} field populated and verified.")
    return True, actual


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
    return text[:limit]


def _classify_login_outcome(driver, auth_network_responses=None, console_entries=None):
    """Return (status, message) or (None, '') if still indeterminate.

    Priority: CORS/EDGE → CAPTCHA/MFA → success → HTTP auth status → visible BTG error → unknown.
    """
    url = (driver.current_url or "").lower()
    try:
        title = driver.title or ""
    except Exception:
        title = ""
    body_raw = _safe_page_text(driver, 4000)
    body = body_raw.lower()
    error_text = _collect_visible_login_errors(driver)
    error_l = (error_text or "").lower()
    combined = f"{body}\n{error_l}"
    auth_network_responses = auth_network_responses or []
    console_blob = "\n".join(
        str(e.get("message", "")).lower() for e in (console_entries or [])
    )

    # CORS / edge failure from browser console — do NOT treat as credential failure
    if (
        "blocked by cors policy" in console_blob
        and "api.businesstalentgroup.com/auth/sign_in" in console_blob
    ) or (
        "api.businesstalentgroup.com/auth/sign_in" in console_blob
        and "net::err_failed" in console_blob
    ):
        return LoginResult.CORS_PREFLIGHT_FAILED, (
            "BTG authentication was not completed because the browser's preflight "
            "request to the BTG authentication API failed. The visible credential "
            "message may be a generic or stale UI response."
        )

    for resp in auth_network_responses:
        try:
            status = int(resp.get("status")) if resp.get("status") is not None else None
        except (TypeError, ValueError):
            status = None
        if status == 403 and "auth/sign_in" in (resp.get("url") or "").lower():
            return LoginResult.BTG_EDGE_403, (
                "BTG edge/WAF returned HTTP 403 for auth/sign_in. "
                "This is not proof that credentials are wrong."
            )
        if status == 429:
            return LoginResult.BTG_RATE_LIMITED, f"Auth HTTP 429 for {resp.get('url', '')}"

    captcha_phrases = ("captcha", "verify you are human", "recaptcha", "hcaptcha", "bot detection")
    if any(p in combined for p in captcha_phrases) or "captcha" in url:
        return LoginResult.CAPTCHA_REQUIRED, "CAPTCHA / bot verification detected — manual action required"

    mfa_phrases = (
        "verification code", "two-factor", "multi-factor", "2-factor",
        "one-time password", "one time password", "authenticator", "enter the code",
    )
    if any(p in combined for p in mfa_phrases):
        return LoginResult.MFA_REQUIRED, "MFA / verification code page detected — manual action required"

    left_login = (
        "login" not in url
        and "sign-in" not in url
        and "signin" not in url
        and "/sign" not in url
    )
    if left_login:
        return LoginResult.SUCCESS, "Redirected away from login"

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

    # Prefer authentication HTTP status when available
    for resp in auth_network_responses:
        status = resp.get("status")
        try:
            status = int(status) if status is not None else None
        except (TypeError, ValueError):
            status = None
        if status == 401:
            return LoginResult.HTTP_401, f"Auth HTTP 401 for {resp.get('url', '')}"
        if status == 403:
            return LoginResult.HTTP_403, f"Auth HTTP 403 for {resp.get('url', '')}"
        if status == 429:
            return LoginResult.HTTP_429, f"Auth HTTP 429 for {resp.get('url', '')}"
        if status is not None and 500 <= status <= 599:
            return LoginResult.HTTP_5XX, f"Auth HTTP {status} for {resp.get('url', '')}"

    if "account locked" in combined or "temporarily locked" in combined or "too many attempts" in combined:
        return LoginResult.ACCOUNT_LOCKED, error_text or "Account locked / too many attempts"

    if "access denied" in combined:
        return LoginResult.ACCESS_DENIED, error_text or "Access denied"

    invalid_combo = (
        "can't find that email address and password combination" in combined
        or "cannot find that email address and password combination" in combined
        or "we can't find that email" in combined
    )
    something_wrong = "something went wrong, please try again later" in combined
    if invalid_combo:
        # If console suggests CORS, that already returned above. Otherwise keep nuance.
        msg = (
            "BTG returned INVALID_CREDENTIALS_RESPONSE "
            "('can't find that email address and password combination'). "
            "This is the platform response and does not by itself prove the "
            "configured password is incorrect — especially when manual login works "
            "and configured/typed fingerprints match."
        )
        if something_wrong:
            msg += " Page also showed 'Something went wrong, please try again later'."
        return LoginResult.INVALID_CREDENTIALS_RESPONSE, msg

    cred_phrases = (
        "invalid", "incorrect", "authentication failed", "unable to sign in",
        "wrong password", "wrong email",
    )
    if error_text or any(p in combined for p in cred_phrases):
        return LoginResult.INVALID_CREDENTIALS, error_text or "Invalid credentials indicated on page"

    if "login" in url or "sign" in url:
        return None, title
    return LoginResult.LOGIN_PAGE_CHANGED, f"Unexpected post-login URL/title: {url} / {title}"


def collect_safe_auth_network_diagnostics(driver):
    """Extract safe auth-related performance log metadata (no bodies/headers/secrets)."""
    results = []
    seen = set()
    auth_terms = (
        "login", "signin", "sign-in", "sign_in", "auth", "authenticate",
        "token", "session", "identity", "oauth", "credential",
    )
    try:
        logs = driver.get_log("performance")
    except Exception as e:
        print(f"  ⚠️ Performance logs unavailable: {e}")
        return results

    for entry in logs:
        try:
            message = json.loads(entry.get("message", "{}")).get("message", {})
            method = message.get("method", "")
            params = message.get("params", {}) or {}
            if method == "Network.responseReceived":
                response = params.get("response", {}) or {}
                url = response.get("url", "") or ""
                if not any(t in url.lower() for t in auth_terms):
                    continue
                status = response.get("status")
                key = (url, response.get("status"), params.get("timestamp"))
                if key in seen:
                    continue
                seen.add(key)
                results.append({
                    "url": url,
                    "method": response.get("requestHeaders", {}).get(":method")
                              or params.get("type")
                              or "",
                    "status": status,
                    "status_text": response.get("statusText", ""),
                    "mime_type": response.get("mimeType", ""),
                    "redirect_url": "",
                    "remote_ip": response.get("remoteIPAddress", ""),
                    "request_timestamp": entry.get("timestamp"),
                    "response_timestamp": params.get("timestamp"),
                })
            elif method == "Network.requestWillBeSent":
                request = params.get("request", {}) or {}
                url = request.get("url", "") or ""
                if not any(t in url.lower() for t in auth_terms):
                    continue
                # Fill method if we later only see response without method
                redirect = params.get("redirectResponse") or {}
                if redirect:
                    rurl = redirect.get("url", "") or url
                    key = (rurl, redirect.get("status"), "redirect")
                    if key not in seen:
                        seen.add(key)
                        results.append({
                            "url": rurl,
                            "method": request.get("method", ""),
                            "status": redirect.get("status"),
                            "status_text": redirect.get("statusText", ""),
                            "mime_type": redirect.get("mimeType", ""),
                            "redirect_url": url,
                            "remote_ip": redirect.get("remoteIPAddress", ""),
                            "request_timestamp": entry.get("timestamp"),
                            "response_timestamp": params.get("timestamp"),
                        })
                # Ensure method is attached to matching response entries later
                for item in results:
                    if item.get("url") == url and not item.get("method"):
                        item["method"] = request.get("method", "")
        except Exception:
            continue

    # Prefer POST auth calls first in output
    results.sort(key=lambda r: (0 if str(r.get("method", "")).upper() == "POST" else 1,
                                str(r.get("url", ""))))
    print(f"  Auth network diagnostics: {len(results)} relevant response(s)")
    for r in results[:8]:
        print(
            f"    {r.get('method') or '?'} {r.get('status')} "
            f"{(r.get('url') or '')[:120]}"
        )
    return results


def collect_browser_console_diagnostics(driver):
    """Collect redacted browser console entries."""
    entries = []
    try:
        logs = driver.get_log("browser")
    except Exception as e:
        print(f"  ⚠️ Browser console logs unavailable: {e}")
        return entries
    for entry in logs:
        entries.append({
            "level": entry.get("level", ""),
            "source": entry.get("source", ""),
            "message": redact_sensitive_text(entry.get("message", "")),
            "timestamp": entry.get("timestamp"),
        })
    print(f"  Browser console entries: {len(entries)}")
    return entries


def _http_status_from_auth_network(auth_network_responses):
    for resp in auth_network_responses or []:
        try:
            status = int(resp.get("status"))
        except (TypeError, ValueError):
            continue
        if status:
            return status
    return None


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
        path = _cookie_file_path()
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
        path = _cookie_file_path()
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


def invalidate_saved_btg_session(driver=None):
    """Delete saved cookies and fully clear the browser session. Returns structured status."""
    status = empty_session_cleanup_status()

    # 1) MongoDB cookie document
    try:
        _get_session_collection().delete_one({"_id": "btg_cookies"})
        status["mongo_cookie_deleted"] = True
        print("  Cleared stale cookies from MongoDB")
    except Exception as e:
        print(f"  ⚠️ Could not clear MongoDB cookies: {e}")
        send_error_notification(
            "COOKIE_CLEAR_FAILURE",
            e,
            details="Failed to delete expired BTG cookies from MongoDB.",
        )

    # 2) Local cookie file
    try:
        path = _cookie_file_path()
        if os.path.exists(path):
            os.remove(path)
            status["local_cookie_file_deleted"] = True
            print("  Cleared stale local cookie backup")
        else:
            status["local_cookie_file_deleted"] = True  # nothing to delete
            print("  Local cookie backup already absent")
    except Exception as e:
        print(f"  ⚠️ Could not clear local cookie backup: {e}")
        send_error_notification(
            "COOKIE_CLEAR_FAILURE",
            e,
            details="Failed to delete expired local BTG cookie backup.",
        )

    if not driver:
        print(f"  Session cleanup status: {status}")
        return status

    # 3) Open origin before clearing storage
    try:
        driver.get(Config.BASE_URL)
        time.sleep(1)
    except Exception as e:
        print(f"  ⚠️ Could not open BASE_URL for session clear: {e}")

    # 4) Selenium cookies
    try:
        driver.delete_all_cookies()
        status["selenium_cookies_cleared"] = True
    except Exception as e:
        print(f"  ⚠️ delete_all_cookies failed: {e}")

    # 5) local/session storage
    try:
        driver.execute_script(
            """
            try { window.localStorage.clear(); } catch (e) {}
            try { window.sessionStorage.clear(); } catch (e) {}
            """
        )
        status["local_storage_cleared"] = True
        status["session_storage_cleared"] = True
    except Exception as e:
        print(f"  ⚠️ local/sessionStorage clear failed: {e}")

    # 6) CDP cookies / cache
    try:
        driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
        status["cdp_cookies_cleared"] = True
    except Exception as e:
        print(f"  ⚠️ CDP clearBrowserCookies failed: {e}")

    try:
        driver.execute_cdp_cmd("Network.clearBrowserCache", {})
        status["browser_cache_cleared"] = True
    except Exception as e:
        print(f"  ⚠️ CDP clearBrowserCache failed: {e}")

    print(f"  Session cleanup status: {json.dumps(status)}")
    return status


# Backward-compatible aliases
def invalidate_saved_cookies():
    return invalidate_saved_btg_session(driver=None)


def clear_stale_cookies():
    return invalidate_saved_btg_session(driver=None)


def clear_btg_browser_session(driver):
    return invalidate_saved_btg_session(driver=driver)


def _login_failure_alert(driver, result, diagnostics, evidence_prefix="btg_login_failure"):
    """Persist evidence and email a detailed login failure (cooldown applies)."""
    current_url = ""
    page_title = ""
    page_text = ""
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

    session_cleanup = diagnostics.get("session_cleanup") or diagnostics.get("browser_session_cleared") or empty_session_cleanup_status()
    auth_network = diagnostics.get("auth_network_responses") or []
    console_entries = diagnostics.get("browser_console_entries") or []

    use_headless = Config.HEADLESS and not Config.BTG_LOGIN_DIAGNOSTIC_MODE
    safe_diag = {
        "result": result.status,
        "message": result.message,
        "current_url": current_url,
        "page_title": page_title,
        "headless": use_headless,
        "diagnostic_mode": Config.BTG_LOGIN_DIAGNOSTIC_MODE,
        "native_user_agent": diagnostics.get("native_user_agent", ""),
        "browser_platform": diagnostics.get("native_platform", ""),
        "navigator_webdriver": diagnostics.get("navigator_webdriver"),
        "email_field_found": diagnostics.get("email_found"),
        "password_field_found": diagnostics.get("password_found"),
        "submit_button_found": diagnostics.get("submit_found"),
        "submit_button_enabled": diagnostics.get("submit_enabled"),
        "submitted": diagnostics.get("submitted"),
        "configured_password_length": diagnostics.get("configured_password_length"),
        "typed_password_length": diagnostics.get("typed_password_length"),
        "configured_password_fingerprint": diagnostics.get("configured_password_fingerprint"),
        "typed_password_fingerprint": diagnostics.get("typed_password_fingerprint"),
        "password_values_match": diagnostics.get("password_values_match"),
        "email_whitespace": diagnostics.get("email_surrounding_whitespace"),
        "password_whitespace": diagnostics.get("password_surrounding_whitespace"),
        "session_cleanup": session_cleanup,
        "auth_network_responses": auth_network,
        "browser_console_error_count": sum(
            1 for e in console_entries if str(e.get("level", "")).upper() in ("SEVERE", "ERROR")
        ),
        "captcha_detected": result.status == LoginResult.CAPTCHA_REQUIRED,
        "mfa_detected": result.status == LoginResult.MFA_REQUIRED,
        "visible_error": diagnostics.get("visible_error", ""),
        "auth_http_status": _http_status_from_auth_network(auth_network),
    }

    paths = {"png": "", "html": "", "json": "", "network": "", "console": ""}
    if driver:
        try:
            paths = save_login_failure_evidence(
                driver,
                prefix=evidence_prefix,
                diagnostics=safe_diag,
                network_responses=auth_network if Config.BTG_CAPTURE_NETWORK_LOGS or auth_network else None,
                console_entries=console_entries if console_entries else None,
            )
        except Exception as e:
            print(f"  ⚠️ Evidence capture failed: {e}")

    details_parts = [
        f"Login result: {result.status}",
        f"Message: {result.message}",
        f"Native user-agent: {diagnostics.get('native_user_agent', '')}",
        f"navigator.webdriver: {diagnostics.get('navigator_webdriver')}",
        f"Email field found: {diagnostics.get('email_found')}",
        f"Password field found: {diagnostics.get('password_found')}",
        f"Submit button found: {diagnostics.get('submit_found')}",
        f"Submit button enabled: {diagnostics.get('submit_enabled')}",
        f"Form submission attempted: {diagnostics.get('submitted')}",
        f"Configured password length: {diagnostics.get('configured_password_length')}",
        f"Typed password length: {diagnostics.get('typed_password_length')}",
        f"Configured password fingerprint: {diagnostics.get('configured_password_fingerprint')}",
        f"Typed password fingerprint: {diagnostics.get('typed_password_fingerprint')}",
        f"Configured and typed passwords match: {diagnostics.get('password_values_match')}",
        f"Email surrounding whitespace: {diagnostics.get('email_surrounding_whitespace')}",
        f"Password surrounding whitespace: {diagnostics.get('password_surrounding_whitespace')}",
        f"Session cleanup: {json.dumps(session_cleanup)}",
        f"Auth HTTP status: {safe_diag.get('auth_http_status')}",
        f"Auth network responses: {len(auth_network)}",
        f"Browser console errors: {safe_diag.get('browser_console_error_count')}",
        f"CAPTCHA detected: {result.status == LoginResult.CAPTCHA_REQUIRED}",
        f"MFA detected: {result.status == LoginResult.MFA_REQUIRED}",
        f"Screenshot: {paths.get('png') or 'n/a'}",
        f"HTML: {paths.get('html') or 'n/a'}",
        f"JSON: {paths.get('json') or 'n/a'}",
        f"Network JSON: {paths.get('network') or 'n/a'}",
        f"Console JSON: {paths.get('console') or 'n/a'}",
        "",
        "Note: BTG invalid-combination responses do not prove the configured password "
        "is wrong when fingerprints match and manual login works.",
        "",
        "Visible page text (truncated, no secrets):",
        page_text or "(unavailable)",
    ]
    attachments = [p for p in paths.values() if p]
    send_error_notification(
        f"LOGIN_FAILURE:{result.status}",
        result.message or result.status,
        details="\n".join(details_parts),
        attachments=attachments,
        extra_rows=[
            ("Current URL", current_url),
            ("Page title", page_title),
            ("Result classification", result.status),
            ("Native user-agent", diagnostics.get("native_user_agent", "")),
            ("navigator.webdriver", diagnostics.get("navigator_webdriver")),
            ("Configured pw length", diagnostics.get("configured_password_length")),
            ("Typed pw length", diagnostics.get("typed_password_length")),
            ("Configured pw fingerprint", diagnostics.get("configured_password_fingerprint")),
            ("Typed pw fingerprint", diagnostics.get("typed_password_fingerprint")),
            ("Passwords match", diagnostics.get("password_values_match")),
            ("Email whitespace", diagnostics.get("email_surrounding_whitespace")),
            ("Password whitespace", diagnostics.get("password_surrounding_whitespace")),
            ("Session cleanup", json.dumps(session_cleanup)),
            ("Auth HTTP status", safe_diag.get("auth_http_status")),
            ("Visible login error", diagnostics.get("visible_error") or result.message),
            ("Evidence PNG", paths.get("png") or "n/a"),
            ("Evidence HTML", paths.get("html") or "n/a"),
            ("Evidence JSON", paths.get("json") or "n/a"),
        ],
    )
    return paths


def perform_login(driver, cookies_invalidated=False, browser_session_cleared=None):
    """Log in to BTG with Angular-aware form fill and classified outcomes."""
    session_cleanup = browser_session_cleared or empty_session_cleanup_status()
    if cookies_invalidated and not any(session_cleanup.values()):
        # Mark mongo/local deleted when caller only invalidated saved cookies
        session_cleanup = dict(session_cleanup)
        session_cleanup["mongo_cookie_deleted"] = True
        session_cleanup["local_cookie_file_deleted"] = True

    diagnostics = {
        "email_found": False,
        "password_found": False,
        "submit_found": False,
        "submit_enabled": False,
        "submitted": False,
        "visible_error": "",
        "cookies_invalidated": bool(cookies_invalidated),
        "browser_session_cleared": session_cleanup,
        "session_cleanup": session_cleanup,
        "native_user_agent": "",
        "native_platform": "",
        "native_language": "",
        "navigator_webdriver": None,
        "auth_network_responses": [],
        "browser_console_entries": [],
    }

    # Do not silently strip passwords — report whitespace instead
    email_missing = Config.BTG_EMAIL is None or Config.BTG_EMAIL == ""
    password_missing = Config.BTG_PASSWORD is None or Config.BTG_PASSWORD == ""
    missing = []
    if email_missing:
        missing.append("BTG_EMAIL")
    if password_missing:
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
        browser_info = get_native_browser_info(driver)
        diagnostics["native_user_agent"] = browser_info.get("userAgent", "")
        diagnostics["native_platform"] = browser_info.get("platform", "")
        diagnostics["native_language"] = browser_info.get("language", "")
        diagnostics["navigator_webdriver"] = browser_info.get("webdriver")
        if diagnostics["native_user_agent"]:
            print(f"  Native user-agent: {diagnostics['native_user_agent']}")
            print(f"  Native platform: {diagnostics['native_platform']}")
            print(f"  Native language: {diagnostics['native_language']}")
            print(f"  navigator.webdriver: {diagnostics['navigator_webdriver']}")
            if browser_info.get("languages"):
                print(f"  Native languages: {browser_info.get('languages')}")

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
                WebDriverWait(driver, 5).until(lambda d: email_field.is_enabled())
                break
            except TimeoutException:
                email_field = None
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
        email_ok, typed_email = _fill_input_field(
            driver, email_field, Config.BTG_EMAIL, "Email"
        )
        if not email_ok:
            cred = credential_diagnostics(Config.BTG_EMAIL, Config.BTG_PASSWORD, "")
            diagnostics.update(cred)
            result = LoginResult(
                LoginResult.FORM_VALUE_MISMATCH,
                "Email field value did not exactly match configured BTG_EMAIL after typing",
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
                WebDriverWait(driver, 5).until(lambda d: password_field.is_enabled())
                break
            except (TimeoutException, NoSuchElementException):
                password_field = None
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
        pw_ok, typed_password = _fill_input_field(
            driver, password_field, Config.BTG_PASSWORD, "Password", is_password=True
        )
        cred = credential_diagnostics(Config.BTG_EMAIL, Config.BTG_PASSWORD, typed_password)
        diagnostics.update(cred)

        if not pw_ok:
            result = LoginResult(
                LoginResult.FORM_VALUE_MISMATCH,
                "Password field value did not exactly match configured BTG_PASSWORD after typing",
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
            WebDriverWait(driver, 10).until(
                lambda d: submit_btn.is_displayed() and submit_btn.is_enabled()
            )
            submit_btn.click()
            print("  Login submitted through button click.")
        except (ElementClickInterceptedException, Exception) as click_err:
            print(f"  Normal click failed ({type(click_err).__name__}) — trying JS click")
            try:
                driver.execute_script("arguments[0].click();", submit_btn)
                print("  Login submitted through JS button click.")
            except Exception as js_err:
                result = LoginResult(
                    LoginResult.UNKNOWN_LOGIN_FAILURE,
                    f"Could not click submit button: {js_err}",
                )
                _login_failure_alert(driver, result, diagnostics)
                return result

        diagnostics["submitted"] = True

        # Wait up to 30s for a page-level outcome, then enrich with network logs once
        deadline = time.time() + 30
        last_status = None
        last_message = ""
        while time.time() < deadline:
            status, message = _classify_login_outcome(driver, [])
            if status == LoginResult.SUCCESS:
                save_cookies(driver)
                print(f"  Login result: {LoginResult.SUCCESS}")
                print(f"✅ Login successful → {driver.current_url}")
                return LoginResult(LoginResult.SUCCESS, message, details=dict(diagnostics))
            if status is not None:
                last_status, last_message = status, message
                break
            time.sleep(0.5)
        else:
            last_status = LoginResult.LOGIN_TIMEOUT
            last_message = (
                f"Timeout waiting for login result. Still at: {driver.current_url}"
            )

        auth_network = []
        if Config.BTG_CAPTURE_NETWORK_LOGS or TEST_BTG_LOGIN_MODE:
            try:
                auth_network = collect_safe_auth_network_diagnostics(driver)
            except Exception as e:
                print(f"  ⚠️ Auth network collection failed: {e}")
        diagnostics["auth_network_responses"] = auth_network

        try:
            diagnostics["browser_console_entries"] = collect_browser_console_diagnostics(driver)
        except Exception:
            diagnostics["browser_console_entries"] = []

        # Re-classify with network + console priority (CORS before credential message)
        status, message = _classify_login_outcome(
            driver,
            auth_network,
            diagnostics.get("browser_console_entries") or [],
        )
        if status == LoginResult.SUCCESS:
            save_cookies(driver)
            print(f"  Login result: {LoginResult.SUCCESS}")
            print(f"✅ Login successful → {driver.current_url}")
            set_monitor_state("authenticated", last_login_result=LoginResult.SUCCESS)
            return LoginResult(LoginResult.SUCCESS, message, details=dict(diagnostics))
        if status is not None:
            last_status, last_message = status, message

        diagnostics["visible_error"] = _collect_visible_login_errors(driver) or last_message
        print(f"  Authentication response: {last_status}")
        print(
            f"  Configured and typed password values match: "
            f"{diagnostics.get('password_values_match')}"
        )
        print(f"❌ Login failed ({last_status}): {last_message}")
        set_monitor_state("degraded", last_login_result=last_status)
        result = LoginResult(last_status, last_message, details=dict(diagnostics))
        _login_failure_alert(
            driver,
            result,
            diagnostics,
            evidence_prefix="btg_local_login" if TEST_BTG_LOGIN_MODE else "btg_login_failure",
        )
        cleanup_old_evidence()
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


def _parse_posted_date(time_str):
    """Return a calendar date for a BTG posted-date value, or None."""
    normalized = _normalize_posted_date(time_str)
    if not normalized:
        return None
    try:
        return datetime.strptime(normalized, "%m/%d/%Y").date()
    except ValueError:
        return None


def make_dedupe_key(project_id, time_posted):
    """Unique occurrence key; project_id itself remains non-unique."""
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

def get_project_history():
    """Return exact keys, known IDs, and latest parsed date for each ID."""
    try:
        docs = _get_collection().find(
            {}, {"project_id": 1, "time_posted": 1, "dedupe_key": 1, "_id": 0}
        )
        keys = set()
        known_ids = set()
        latest_by_id = {}
        for d in docs:
            project_id = d.get("project_id")
            if not project_id:
                continue
            known_ids.add(project_id)

            # Older docs lack dedupe_key — rebuild it from stored fields
            key = d.get("dedupe_key") or make_dedupe_key(
                project_id, d.get("time_posted")
            )
            if key:
                keys.add(key)

            posted_date = _parse_posted_date(d.get("time_posted"))
            current_latest = latest_by_id.get(project_id)
            if posted_date and (current_latest is None or posted_date > current_latest):
                latest_by_id[project_id] = posted_date
        return keys, known_ids, latest_by_id
    except Exception as e:
        print(f"DB project-history load failed: {e}")
        raise


def get_seen_ids():
    """Compatibility helper returning occurrence keys already in DB."""
    return get_project_history()[0]

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
        return True
    except Exception as e:
        print(f"⚠️ DB bulk insert failed: {e}")
        return False


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


def filter_new_projects(all_projects, seen_ids, known_ids=None, latest_by_id=None):
    """Keep first-seen IDs and re-posts newer than the configured day gap.

    ``project_id`` is the conditional lookup key. ``dedupe_key`` remains the
    unique occurrence key, preventing duplicate alerts for the same ID/date.
    """
    known_ids = known_ids if known_ids is not None else set()
    latest_by_id = latest_by_id if latest_by_id is not None else {}
    comparison_keys = set(seen_ids)
    comparison_ids = set(known_ids)
    comparison_latest = dict(latest_by_id)
    result = []
    for p in all_projects:
        project_id = p.get("id")
        if not project_id:
            continue

        posted_value = p.get("time_posted", "")
        key = make_dedupe_key(project_id, posted_value)
        if key in comparison_keys:
            continue

        current_date = _parse_posted_date(posted_value)
        if project_id not in comparison_ids:
            result.append(p)
            comparison_keys.add(key)
            comparison_ids.add(project_id)
            if current_date:
                comparison_latest[project_id] = current_date
            continue

        latest_date = comparison_latest.get(project_id)
        if current_date is None or latest_date is None:
            print(
                f"  Skipping known project {project_id}: "
                "posted date cannot be compared safely"
            )
            continue

        gap_days = (current_date - latest_date).days
        if gap_days > Config.REPOST_MIN_DAYS:
            result.append(p)
            comparison_keys.add(key)
            comparison_latest[project_id] = current_date
        elif DEBUG_MODE:
            print(
                f"  Skipping project {project_id}: repost gap is "
                f"{gap_days} day(s), requires > {Config.REPOST_MIN_DAYS}"
            )
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
    global _chrome_profile_dir
    print_browser_startup_diagnostics()
    prepare_chromedriver_log()

    options = Options()
    use_headless = Config.HEADLESS and not Config.BTG_LOGIN_DIAGNOSTIC_MODE
    if Config.BTG_LOGIN_DIAGNOSTIC_MODE:
        print("  Local diagnostic mode is active (headed Chrome)")
        print("  ⚠️ BTG_LOGIN_DIAGNOSTIC_MODE=true — headless disabled for diagnostics")
    if use_headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-software-rasterizer")
    if Config.BTG_LOGIN_DIAGNOSTIC_MODE:
        options.add_argument("--window-size=1440,1000")
    else:
        options.add_argument("--window-size=1920,1080")

    # Unique temp profile per browser instance (Railway ephemeral FS safe)
    _chrome_profile_dir = tempfile.mkdtemp(prefix="btg-chrome-")
    options.add_argument(f"--user-data-dir={_chrome_profile_dir}")

    if Config.BTG_CAPTURE_NETWORK_LOGS or TEST_BTG_LOGIN_MODE:
        options.set_capability("goog:loggingPrefs", {
            "performance": "ALL",
            "browser": "ALL",
        })
        print("  Chrome performance/browser logs enabled")

    chrome_bin = _find_binary("CHROME_BIN", [
        Config.CHROME_BIN,
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ])
    if chrome_bin:
        options.binary_location = chrome_bin
        print(f"  Chrome binary: {chrome_bin}")

    system_path = _find_binary("CHROMEDRIVER_PATH", [
        Config.CHROMEDRIVER_PATH,
        "/usr/bin/chromedriver",
        "/usr/lib/chromium/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
    ])
    if system_path:
        service = create_chromedriver_service(system_path)
        print(f"  Chromedriver (system): {system_path}")
    else:
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
        _cleanup_chrome_profile()
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

    if Config.BTG_LOGIN_DIAGNOSTIC_MODE:
        try:
            driver.set_window_size(1440, 1000)
        except Exception:
            pass

    if Config.BTG_CAPTURE_NETWORK_LOGS or TEST_BTG_LOGIN_MODE:
        try:
            driver.execute_cdp_cmd("Network.enable", {})
        except Exception:
            pass

    browser_info = get_native_browser_info(driver)
    print(f"  Native user-agent: {browser_info.get('userAgent') or '(unavailable)'}")
    print(f"  Native platform: {browser_info.get('platform') or '(unavailable)'}")
    print(f"  Native language: {browser_info.get('language') or '(unavailable)'}")
    print(f"  navigator.webdriver: {browser_info.get('webdriver')}")
    return driver


def _cleanup_chrome_profile():
    global _chrome_profile_dir
    if not _chrome_profile_dir:
        return
    try:
        shutil.rmtree(_chrome_profile_dir, ignore_errors=True)
    except Exception:
        pass
    _chrome_profile_dir = None


def _safe_quit(driver):
    if not driver:
        _cleanup_chrome_profile()
        return
    try:
        driver.quit()
    except Exception:
        pass
    _cleanup_chrome_profile()


def setup_session(driver):
    """Try cookies first, fall back to login. Returns LoginResult."""
    cookies_invalidated = False
    browser_cleared = empty_session_cleanup_status()

    if Config.BTG_CLEAR_SESSION_ON_START or TEST_BTG_LOGIN_MODE:
        print("  BTG_CLEAR_SESSION_ON_START — wiping saved + browser session before login")
        browser_cleared = invalidate_saved_btg_session(driver)
        cookies_invalidated = True
        return perform_login(
            driver,
            cookies_invalidated=cookies_invalidated,
            browser_session_cleared=browser_cleared,
        )

    if load_cookies(driver):
        driver.get(Config.PROJECTS_URL)
        time.sleep(5)
        # Check we're not kicked back to login
        url = (driver.current_url or "").lower()
        if "login" not in url and "sign" not in url:
            print("✅ Logged in via cookies")
            return LoginResult(LoginResult.SUCCESS, "Logged in via cookies")
        print("  Cookies expired — invalidating saved session and clearing browser...")
        cookies_invalidated = True
        browser_cleared = invalidate_saved_btg_session(driver)
        # Do not email merely because cookies expired if fresh login succeeds

    return perform_login(
        driver,
        cookies_invalidated=cookies_invalidated,
        browser_session_cleared=browser_cleared,
    )


# ============================
# MAIN MONITORING LOOP
# ============================
def _alert_zero_projects(driver, streak):
    png_path, html_path, json_path = "", "", ""
    current_url = ""
    try:
        current_url = driver.current_url
    except Exception:
        pass
    try:
        paths = save_login_failure_evidence(
            driver, prefix="btg_zero_projects"
        )
        png_path = paths.get("png", "")
        html_path = paths.get("html", "")
        json_path = paths.get("json", "")
    except Exception as e:
        print(f"  ⚠️ Zero-project evidence capture failed: {e}")
        png_path = html_path = json_path = ""
    send_error_notification(
        "ZERO_PROJECTS_EXTRACTED",
        f"No project cards extracted for {streak} consecutive scan(s)",
        details=(
            f"Projects page returned zero cards after retry.\n"
            f"URL: {current_url}\n"
            f"Screenshot: {png_path or 'n/a'}\n"
            f"HTML: {html_path or 'n/a'}\n"
            f"JSON: {json_path or 'n/a'}\n\n"
            f"Visible text (truncated):\n{_safe_page_text(driver, 1500)}"
        ),
        attachments=[p for p in (png_path, html_path, json_path) if p],
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
        known_ids = set()
        latest_by_id = {}
        cold_start_pending = False
        print("🧪 DB skipped — running in-memory only\n")
    else:
        try:
            cold_start_pending = db_is_cold_start()
            init_db()
            seen_ids, known_ids, latest_by_id = get_project_history()
            print(
                f"📁 DB loaded — {len(seen_ids)} occurrence(s) across "
                f"{len(known_ids)} project ID(s)\n"
            )
        except Exception as e:
            send_error_notification(
                "MONGODB_CONNECTION_FAILURE",
                e,
                details="Failed during MongoDB init / seen-id load.",
                traceback_text=traceback_mod.format_exc(),
            )
            raise

        if cold_start_pending:
            print(
                "⚙️  First run detected — the first successful scan will be "
                "seeded without sending project emails.\n"
            )

    last_keepalive = time.time()
    KEEPALIVE_INTERVAL = 1800  # refresh session every 30 minutes

    while not shutdown_event.is_set():
        try:
            renew_worker_lock()
            set_monitor_state("scanning")
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
            interruptible_sleep(5)
            if shutdown_event.is_set():
                return "stop"

            # If session expired, BTG silently redirects to /login — re-login immediately
            url = (driver.current_url or "").lower()
            if "login" in url or "sign" in url:
                print("  ⚠️ Session expired — clearing stale cookies/session and re-logging in...")
                browser_cleared = invalidate_saved_btg_session(driver)
                login_result = perform_login(
                    driver,
                    cookies_invalidated=True,
                    browser_session_cleared=browser_cleared,
                )
                if not login_result.ok:
                    print(f"  Authentication response: {login_result.status}")
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
                set_monitor_state("sleeping")
                interruptible_sleep(Config.CHECK_INTERVAL)
                continue

            _zero_project_streak = 0
            set_monitor_state(
                "scanning",
                last_successful_scan=datetime.now(PKT).isoformat(),
                status="ok",
            )

            # Never alert on the first run. Keep trying to seed until MongoDB
            # confirms the initial visible projects were stored successfully.
            if cold_start_pending and not TEST_MODE:
                print("⚙️  Seeding first successful scan (project emails suppressed)...")
                if bulk_insert_projects(all_projects, emailed=False):
                    seen_ids, known_ids, latest_by_id = get_project_history()
                    cold_start_pending = False
                    print(
                        f"✅ Seeded {len(all_projects)} existing project(s). "
                        "Only qualifying future posts will trigger emails.\n"
                    )
                else:
                    print(
                        "⚠️  Initial seed was not confirmed; project emails remain "
                        "suppressed and seeding will retry next cycle.\n"
                    )
                if ONCE_MODE:
                    print("\n✅ Once mode complete after first-run seed. Exiting.")
                    return "once"
                set_monitor_state("sleeping")
                interruptible_sleep(Config.CHECK_INTERVAL)
                continue

            new_projects = filter_new_projects(
                all_projects,
                seen_ids,
                known_ids,
                latest_by_id,
            )

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
                    project_id = project["id"]
                    known_ids.add(project_id)
                    posted_date = _parse_posted_date(project.get("time_posted"))
                    current_latest = latest_by_id.get(project_id)
                    if posted_date and (
                        current_latest is None or posted_date > current_latest
                    ):
                        latest_by_id[project_id] = posted_date
            else:
                print("⏳ No new projects this cycle")

            print(f"📊 Stats: {len(all_projects)} visible, {len(seen_ids)} in DB")

            if ONCE_MODE:
                print("\n✅ Once mode complete. Exiting.")
                return "once"

            print(f"\n⏳ Next check in {Config.CHECK_INTERVAL}s...")
            set_monitor_state("sleeping")
            interruptible_sleep(Config.CHECK_INTERVAL)

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
            interruptible_sleep(Config.LOGIN_RETRY_INTERVAL)
            if shutdown_event.is_set():
                return "stop"
            driver = initialize_driver()
            login_result = setup_session(driver)
            if not login_result.ok:
                print(
                    f"Authentication unavailable. Next login attempt in "
                    f"{Config.LOGIN_RETRY_INTERVAL} seconds."
                )
                _safe_quit(driver)
                return "auth_retry"

    return "stop"


def main():
    install_signal_handlers()
    set_monitor_state("starting")
    start_health_server()

    print("=" * 50)
    print("🚀 BTG Project Monitor")
    if DEBUG_MODE:
        print("   (DEBUG MODE ON — page structure will be printed)")
    print("=" * 50)
    if is_railway_environment():
        meta = railway_metadata()
        print(f"  Railway env : {meta.get('environment') or '(set)'}")
        print(f"  Railway svc : {meta.get('service') or '(unknown)'}")
        print(f"  Railway region: {meta.get('region') or '(unknown)'}")
        print(f"  Railway deploy: {meta.get('deployment_id') or '(unknown)'}")
    print(f"  Account  : {Config.BTG_EMAIL}")
    print(f"  Interval : {Config.CHECK_INTERVAL}s")
    print(f"  Login retry: {Config.LOGIN_RETRY_INTERVAL}s")
    print(f"  Repost alert gap: > {Config.REPOST_MIN_DAYS} days")
    print(f"  Preflight: {'enabled' if Config.BTG_PREFLIGHT_ENABLED else 'disabled'}")
    print("  Max age  : disabled (all unseen projects are saved & emailed)")
    print(f"  Recipients: {', '.join(Config.RECIPIENT_EMAILS)}")
    if Config.ERROR_RECIPIENTS:
        print(f"  Error alerts: {', '.join(Config.ERROR_RECIPIENTS)}")
    else:
        print("  Error alerts: NOT CONFIGURED (set error_recipent)")
    print(f"  Error cooldown: {Config.ERROR_EMAIL_COOLDOWN_MINUTES} minutes")
    print(f"  Health : 0.0.0.0:{Config.HEALTH_PORT}/health")
    if Config.BTG_LOGIN_DIAGNOSTIC_MODE:
        print("  Login diagnostic mode: ENABLED (headed browser)")
    print()

    ok, missing = validate_configuration()
    if not ok:
        set_monitor_state("degraded", status="degraded")
        log_event("ERROR", "config_invalid", missing=",".join(missing))
        send_error_notification(
            "CONFIGURATION_ERROR",
            f"Missing required environment variable(s): {', '.join(missing)}",
            details="Monitor remains alive with /health degraded. Secrets are never printed.",
            extra_rows=[("Missing variables", ", ".join(missing))],
        )
        while not shutdown_event.is_set():
            interruptible_sleep(300)
        stop_health_server()
        return

    if TEST_MODE:
        Config.RECIPIENT_EMAILS = ["muhammadammar7747@gmail.com"]
        print("🧪 TEST MODE — MongoDB skipped, 1 test email → muhammadammar7747@gmail.com\n")

    while not shutdown_event.is_set():
        driver = None
        try:
            if not acquire_worker_lock():
                set_monitor_state("sleeping")
                log_event("INFO", "worker_standby", message="another replica holds the lock")
                interruptible_sleep(min(Config.CHECK_INTERVAL, 60))
                continue

            if Config.BTG_PREFLIGHT_ENABLED:
                set_monitor_state("preflight_check")
                preflight = check_btg_auth_preflight()
                set_monitor_state(
                    "preflight_check" if preflight.get("ok") else "degraded",
                    last_preflight=preflight,
                    status="ok" if preflight.get("ok") else "degraded",
                )
                if not preflight.get("ok"):
                    alert_preflight_failure(preflight)
                    log_event(
                        "WARN",
                        "preflight_blocked_login",
                        classification=preflight.get("classification"),
                    )
                    interruptible_sleep(Config.BTG_PREFLIGHT_FAILURE_RETRY_SECONDS)
                    continue

            set_monitor_state("logging_in")
            driver = initialize_driver()
            login_result = setup_session(driver)
            set_monitor_state(
                "authenticated" if login_result.ok else "degraded",
                last_login_result=login_result.status,
                status="ok" if login_result.ok else "degraded",
            )
            if not login_result.ok:
                print(f"❌ Failed to establish BTG session ({login_result.status})")
                print(
                    f"Authentication unavailable. Next login attempt in "
                    f"{Config.LOGIN_RETRY_INTERVAL} seconds."
                )
                _safe_quit(driver)
                driver = None
                interruptible_sleep(Config.LOGIN_RETRY_INTERVAL)
                continue

            driver.get(Config.PROJECTS_URL)
            interruptible_sleep(4)
            if shutdown_event.is_set():
                break

            set_monitor_state("scanning")
            outcome = run_monitoring_loop(driver)
            _safe_quit(driver)
            driver = None
            cleanup_old_evidence()

            if outcome == "once" or ONCE_MODE:
                print("✅ BTG Monitor stopped")
                break
            if outcome == "auth_retry":
                print(
                    f"Authentication unavailable. Next login attempt in "
                    f"{Config.LOGIN_RETRY_INTERVAL} seconds."
                )
                interruptible_sleep(Config.LOGIN_RETRY_INTERVAL)
                continue

            print(
                f"Monitoring loop ended ({outcome}). "
                f"Retrying in {Config.LOGIN_RETRY_INTERVAL} seconds..."
            )
            interruptible_sleep(Config.LOGIN_RETRY_INTERVAL)

        except KeyboardInterrupt:
            request_shutdown(signal.SIGINT, None)
            _safe_quit(driver)
            break
        except Exception as e:
            print(f"\n❌ Unexpected error: {e}")
            traceback_mod.print_exc()
            set_monitor_state("degraded", status="degraded")
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
            interruptible_sleep(Config.LOGIN_RETRY_INTERVAL)

    set_monitor_state("shutting_down")
    release_worker_lock()
    stop_health_server()
    print("✅ BTG Monitor stopped")



def run_test_btg_login():
    """One-shot local BTG login diagnostic. Returns exit code 0/1/2."""
    print("=" * 60)
    print("BTG local login diagnostic (--test-btg-login)")
    print("=" * 60)

    email_missing = Config.BTG_EMAIL is None or Config.BTG_EMAIL == ""
    password_missing = Config.BTG_PASSWORD is None or Config.BTG_PASSWORD == ""
    if email_missing or password_missing:
        missing = []
        if email_missing:
            missing.append("BTG_EMAIL")
        if password_missing:
            missing.append("BTG_PASSWORD")
        print(f"Invalid configuration — missing: {', '.join(missing)}")
        return 2

    print(f"  Diagnostic mode: {Config.BTG_LOGIN_DIAGNOSTIC_MODE}")
    print(f"  Clear session on start: True (forced for this command)")
    print(f"  Capture network logs: True (forced for this command)")
    print(
        f"  Pause after failure: "
        f"{Config.BTG_PAUSE_AFTER_LOGIN_FAILURE and Config.BTG_LOGIN_DIAGNOSTIC_MODE}"
    )
    print(f"  Headless effective: {Config.HEADLESS and not Config.BTG_LOGIN_DIAGNOSTIC_MODE}")
    print(f"  Preflight enabled: {Config.BTG_PREFLIGHT_ENABLED}")

    # Force diagnostic capture for this one-shot command
    Config.BTG_CLEAR_SESSION_ON_START = True
    Config.BTG_CAPTURE_NETWORK_LOGS = True

    if Config.BTG_PREFLIGHT_ENABLED or is_railway_environment():
        preflight = check_btg_auth_preflight()
        print("Preflight result:")
        print(json.dumps(preflight, indent=2, default=str))
        if not preflight.get("ok"):
            print("Aborting login because preflight failed.")
            return 1

    driver = None
    try:
        driver = initialize_driver()
        session_status = invalidate_saved_btg_session(driver)
        result = perform_login(
            driver,
            cookies_invalidated=True,
            browser_session_cleared=session_status,
        )
        print("\n--- LOGIN TEST RESULT ---")
        print(f"Classification: {result.status}")
        print(f"Message: {result.message}")
        details = result.details or {}
        print(f"Passwords match: {details.get('password_values_match')}")
        print(f"Configured fingerprint: {details.get('configured_password_fingerprint')}")
        print(f"Typed fingerprint: {details.get('typed_password_fingerprint')}")
        print(f"Session cleanup: {json.dumps(details.get('session_cleanup') or {})}")
        auth_http = _http_status_from_auth_network(details.get("auth_network_responses") or [])
        print(f"Auth HTTP status: {auth_http}")
        print(f"Auth network responses: {len(details.get('auth_network_responses') or [])}")
        print(f"Console entries: {len(details.get('browser_console_entries') or [])}")
        print(f"Native UA: {details.get('native_user_agent')}")

        if result.ok:
            print("Login succeeded.")
            _safe_quit(driver)
            return 0

        if Config.BTG_PAUSE_AFTER_LOGIN_FAILURE and Config.BTG_LOGIN_DIAGNOSTIC_MODE:
            try:
                input("\nLogin failed — browser left open. Press Enter to close and exit...")
            except EOFError:
                print("  (No TTY for pause — continuing)")

        _safe_quit(driver)
        return 1
    except Exception as e:
        print(f"Login test crashed: {e}")
        traceback_mod.print_exc()
        _safe_quit(driver)
        return 1


if __name__ == "__main__":
    if PRINT_RUNTIME_DIAGNOSTICS_MODE:
        print_runtime_diagnostics()
        sys.exit(0)

    if TEST_ERROR_EMAIL_MODE:
        ok = run_test_error_email()
        sys.exit(0 if ok else 1)

    if TEST_BTG_PREFLIGHT_MODE:
        sys.exit(run_test_btg_preflight())

    if TEST_BTG_LOGIN_MODE:
        code = run_test_btg_login()
        sys.exit(code)

    try:
        main()
    except KeyboardInterrupt:
        print("\n⏹️  Stopped by user")
        release_worker_lock()
        stop_health_server()
    except Exception as fatal:
        print(f"💥 Fatal crash: {fatal}")
        send_error_notification(
            "FATAL_OUTER_CRASH",
            fatal,
            details="Unhandled exception escaped main().",
            traceback_text=traceback_mod.format_exc(),
            force=True,
        )
        release_worker_lock()
        stop_health_server()
        sys.exit(1)
