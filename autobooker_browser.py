import os, sys, subprocess, logging, re
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

# Self-contained browser bootstrap so only this file needs to be replaced.
def ensure_playwright():
    try:
        import playwright  # noqa
        return
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--no-cache-dir", "playwright>=1.50,<2"])

ensure_playwright()
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

TZ = ZoneInfo(os.getenv("TIMEZONE", "America/Toronto"))
URL = os.getenv("BOOKING_URL", "https://megaclubsharedfacility.simplybook.me/v2/pwa/?theme=pwa")
LOGIN = os.environ["SIMPLYBOOK_LOGIN"]
PASSWORD = os.environ["SIMPLYBOOK_PASSWORD"]
EMAIL_TO = os.environ["EMAIL_TO"]
EMAIL_FROM = os.getenv("EMAIL_FROM", EMAIL_TO)
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", EMAIL_FROM)
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
BADMINTON_DAYS = {0, 4, 5, 6}

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)

def visible_text(page):
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""

def click_text(page, patterns, timeout=5000):
    for pat in patterns:
        try:
            loc = page.get_by_text(re.compile(pat, re.I)).filter(visible=True).first
            if loc.count():
                loc.click(timeout=timeout)
                return True
        except Exception:
            pass
    return False

def fill_first(page, selectors, value):
    for sel in selectors:
        try:
            loc = page.locator(sel).filter(visible=True).first
            if loc.count():
                loc.fill(value)
                return True
        except Exception:
            pass
    return False

def install_browser():
    # Install Chromium and OS dependencies inside the Render container.
    # This is intentionally self-contained because the existing Render Dockerfile
    # only installs Python dependencies.
    marker = "/tmp/.mega_chromium_ready"
    if os.path.exists(marker):
        return
    logging.info("Installing Playwright Chromium (first run only)...")
    try:
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"])
    except Exception:
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
    open(marker, "w").close()

def login(page):
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)

    # Dismiss common cookie/consent dialogs if present.
    click_text(page, [r"accept", r"agree", r"got it"], timeout=2500)

    body = visible_text(page)
    if re.search(r"log\s*out|sign\s*out|my\s+account|my\s+bookings", body, re.I):
        logging.info("Existing logged-in session detected.")
        return

    # Try common SimplyBook client login controls.
    if not click_text(page, [r"sign\s*in", r"log\s*in", r"login", r"my\s+account"], timeout=7000):
        logging.info("No login button found immediately; inspecting page.")

    page.wait_for_timeout(1500)

    # Try email/login + password fields.
    user_ok = fill_first(page, [
        'input[type="email"]',
        'input[name*="login" i]',
        'input[name*="email" i]',
        'input[placeholder*="email" i]',
        'input[placeholder*="login" i]',
        'input[type="text"]',
    ], LOGIN)
    pass_ok = fill_first(page, [
        'input[type="password"]',
        'input[name*="password" i]',
        'input[placeholder*="password" i]',
    ], PASSWORD)

    if not (user_ok and pass_ok):
        page.screenshot(path="/tmp/mega_login_fields.png", full_page=True)
        raise RuntimeError("Could not find the SimplyBook client login fields. Screenshot saved to /tmp/mega_login_fields.png")

    if not click_text(page, [r"sign\s*in", r"log\s*in", r"login", r"continue"], timeout=7000):
        # Last resort: submit the form.
        try:
            page.locator("form").filter(visible=True).first.press("Enter")
        except Exception:
            raise RuntimeError("Could not submit SimplyBook login form.")

    page.wait_for_timeout(3000)
    body = visible_text(page)
    if re.search(r"incorrect|invalid|wrong password|not found", body, re.I):
        raise RuntimeError("SimplyBook rejected the login credentials.")
    logging.info("Client login submitted.")

def target_window(d):
    return ("19:00", "22:00") if d.weekday() < 5 else ("12:00", "22:00")

def time_minutes(s):
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", s)
    if not m:
        return None
    return int(m.group(1))*60 + int(m.group(2))

def choose_time_from_page(page, d):
    lo_s, hi_s = target_window(d)
    lo = time_minutes(lo_s)
    hi = time_minutes(hi_s)
    candidates = []
    # Read visible buttons/links that look like times.
    for sel in ['button', '[role="button"]', 'a']:
        try:
            for el in page.locator(sel).filter(visible=True).all():
                txt = (el.inner_text() or "").strip()
                tm = time_minutes(txt)
                if tm is not None and lo <= tm <= hi:
                    candidates.append((tm, el, txt))
        except Exception:
            pass
    # De-duplicate by minute, earliest first.
    seen = set()
    for tm, el, txt in sorted(candidates, key=lambda x: x[0]):
        if tm in seen:
            continue
        seen.add(tm)
        try:
            el.click(timeout=3000)
            return txt
        except Exception:
            continue
    return None

def select_date(page, d):
    # Direct date input, if the PWA exposes one.
    for sel in ['input[type="date"]', 'input[placeholder*="date" i]']:
        try:
            loc = page.locator(sel).filter(visible=True).first
            if loc.count():
                loc.fill(d.isoformat())
                loc.press("Enter")
                page.wait_for_timeout(1500)
                return
        except Exception:
            pass

    # Otherwise use calendar controls. Move forward until target month/year is visible.
    target_label = d.strftime("%B %Y")
    for _ in range(15):
        body = visible_text(page)
        if target_label.lower() in body.lower():
            break
        # Common next-month labels/icons.
        clicked = False
        for pat in [r"next", r"next month", r"›", r">"]:
            try:
                loc = page.get_by_text(re.compile(pat, re.I)).filter(visible=True).last
                if loc.count():
                    loc.click(timeout=1500)
                    page.wait_for_timeout(400)
                    clicked = True
                    break
            except Exception:
                pass
        if not clicked:
            # Try buttons with aria-label/title.
            for attr in ["[aria-label*='next' i]", "[title*='next' i]"]:
                try:
                    loc = page.locator(attr).filter(visible=True).first
                    if loc.count():
                        loc.click(timeout=1500)
                        page.wait_for_timeout(400)
                        clicked = True
                        break
                except Exception:
                    pass
        if not clicked:
            break

    # Click the exact day, preferring calendar day buttons.
    day = str(d.day)
    candidates = []
    for sel in ['button', '[role="button"]', 'td', 'a']:
        try:
            for el in page.locator(sel).filter(visible=True).all():
                txt = (el.inner_text() or "").strip()
                aria = el.get_attribute("aria-label") or ""
                title = el.get_attribute("title") or ""
                combined = f"{txt} {aria} {title}"
                if re.fullmatch(day, txt) or re.search(rf"\b{re.escape(d.strftime('%B'))}\s+{day}\b", combined, re.I):
                    candidates.append(el)
        except Exception:
            pass
    for el in candidates:
        try:
            el.click(timeout=2500)
            page.wait_for_timeout(1200)
            return
        except Exception:
            continue
    raise RuntimeError(f"Could not select release date {d.isoformat()}")

def choose_service(page, sport):
    # Mega Club's PWA uses the exact service names shown on screen:
    # "Badminton Court" and "Tennis Court".
    service_name = "Badminton Court" if sport == "Badminton" else "Tennis Court"
    logging.info("Looking for exact service: %s", service_name)

    # Wait briefly for the Service step/cards to finish rendering.
    try:
        page.get_by_text(service_name, exact=True).first.wait_for(state="visible", timeout=10000)
    except Exception:
        pass

    # First try exact text. Do NOT rely on the text being the clickable element:
    # in this PWA the blue arrow/card is the clickable area.
    locators = [
        page.get_by_text(service_name, exact=True).first,
        page.locator(f'text="{service_name}"').first,
        page.locator("body").get_by_text(service_name, exact=True).first,
    ]

    for loc in locators:
        try:
            if not loc.is_visible():
                continue
            loc.scroll_into_view_if_needed(timeout=3000)

            # Click the text itself first.
            try:
                loc.click(timeout=4000)
                page.wait_for_timeout(1200)
                logging.info("Selected service: %s", service_name)
                return True
            except Exception:
                pass

            # Then walk up to likely clickable containers.
            for xp in [
                "xpath=ancestor::*[@role='button'][1]",
                "xpath=ancestor::button[1]",
                "xpath=ancestor::*[contains(@class,'service')][1]",
                "xpath=ancestor::*[contains(@class,'item')][1]",
                "xpath=ancestor::*[contains(@class,'card')][1]",
                "xpath=..",
                "xpath=../..",
            ]:
                try:
                    parent = loc.locator(xp).first
                    if parent.count() and parent.is_visible():
                        parent.scroll_into_view_if_needed(timeout=2000)
                        parent.click(timeout=4000)
                        page.wait_for_timeout(1200)
                        logging.info("Selected service card: %s", service_name)
                        return True
                except Exception:
                    pass
        except Exception:
            pass

    # Diagnostic: the text may be rendered inside an iframe or after a delayed
    # Angular/React update. Log whether it exists anywhere in the DOM.
    body = visible_text(page)
    if service_name.lower() in body.lower():
        logging.info("Service text IS present in page body but could not be clicked: %s", service_name)
    else:
        logging.info("Service text is NOT present in current page body: %s", service_name)
    page.screenshot(path=f"/tmp/mega_service_{sport.lower()}.png", full_page=True)
    return False

def continue_or_confirm(page):
    for pat in [r"continue", r"next", r"book", r"reserve", r"confirm", r"make\s+booking"]:
        if click_text(page, [pat], timeout=2500):
            page.wait_for_timeout(1200)
            return True
    return False

def agree_rules(page):
    for sel in ['input[type="checkbox"]', '[role="checkbox"]']:
        try:
            for el in page.locator(sel).filter(visible=True).all():
                try:
                    checked = el.is_checked()
                except Exception:
                    checked = False
                if not checked:
                    el.check()
                    return True
        except Exception:
            pass
    # Text next to checkbox may be clickable.
    return click_text(page, [r"agree", r"rules", r"terms"], timeout=2500)

def book_sport(page, sport, d):
    logging.info("Trying %s for %s", sport, d)
    if not choose_service(page, sport):
        logging.info("%s service not found on page.", sport)
        return False

    # Service cards often expose a calendar after selection.
    select_date(page, d)
    chosen = choose_time_from_page(page, d)
    if not chosen:
        logging.info("No %s slot in target window.", sport)
        return False

    logging.info("%s candidate selected: %s", sport, chosen)
    if DRY_RUN:
        logging.info("DRY_RUN=true — stopping before final booking.")
        return True

    continue_or_confirm(page)
    page.wait_for_timeout(800)
    agree_rules(page)
    continue_or_confirm(page)
    page.wait_for_timeout(1200)

    # Final confirmation if still present.
    if click_text(page, [r"confirm\s+booking", r"confirm", r"book\s+now", r"reserve"], timeout=4000):
        page.wait_for_timeout(1800)

    body = visible_text(page)
    if re.search(r"booking\s+(confirmed|successful)|successfully\s+booked|thank\s+you", body, re.I):
        logging.info("BOOKING SUCCESS: %s %s %s", sport, d, chosen)
        return chosen

    # A confirmation page may not expose those exact words; inspect for cancellation/no-slot errors.
    if re.search(r"error|failed|not available|already booked", body, re.I):
        raise RuntimeError(f"Booking did not confirm. Page reported an error or unavailable state.")

    # Save a screenshot for diagnosis rather than pretending success.
    page.screenshot(path="/tmp/mega_after_booking.png", full_page=True)
    raise RuntimeError("Could not verify final booking confirmation; screenshot saved to /tmp/mega_after_booking.png")

def send_email(sport, d, chosen):
    import smtplib
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = f"✅ Mega Club booked — {sport} {d.isoformat()} {str(chosen)[:5]}"
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.set_content(
        f"Your Mega Club booking succeeded.\n\n"
        f"Sport: {sport}\nDate: {d.isoformat()}\nStart: {str(chosen)[:5]}\n"
    )
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(msg)

def main():
    override = os.getenv("RELEASE_DATE")
    d = date.fromisoformat(override) if override else (datetime.now(TZ).date() + timedelta(days=7))
    # Production runs should occur around Toronto midnight. Manual runs can set RELEASE_DATE.
    if d.weekday() in BADMINTON_DAYS:
        order = ["Badminton", "Tennis"]
    else:
        order = ["Tennis"]

    install_browser()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1440, "height": 1100})
        page = context.new_page()
        try:
            login(page)
            for sport in order:
                try:
                    result = book_sport(page, sport, d)
                    if result:
                        if not DRY_RUN:
                            send_email(sport, d, result)
                        return
                except Exception:
                    logging.exception("%s attempt failed.", sport)
                    # Reload fresh state before trying Tennis after a failed Badminton attempt.
                    try:
                        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(1500)
                        login(page)
                    except Exception:
                        pass
            logging.info("No suitable booking found for %s.", d)
        finally:
            context.close()
            browser.close()

if __name__ == "__main__":
    main()
