import os
import sys
import subprocess
import logging
import re
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo


# ============================================================
# Playwright bootstrap
# ============================================================

def ensure_playwright():
    try:
        import playwright  # noqa
        return
    except ImportError:
        subprocess.check_call([
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "playwright>=1.50,<2"
        ])


ensure_playwright()

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError
)


# ============================================================
# Configuration
# ============================================================

TZ = ZoneInfo(
    os.getenv("TIMEZONE", "America/Toronto")
)

URL = os.getenv(
    "BOOKING_URL",
    "https://megaclubsharedfacility.simplybook.me/v2/pwa/?theme=pwa"
)

LOGIN = os.environ["SIMPLYBOOK_LOGIN"]
PASSWORD = os.environ["SIMPLYBOOK_PASSWORD"]

EMAIL_TO = os.environ["EMAIL_TO"]
EMAIL_FROM = os.getenv("EMAIL_FROM", EMAIL_TO)

SMTP_HOST = os.getenv(
    "SMTP_HOST",
    "smtp.gmail.com"
)

SMTP_PORT = int(
    os.getenv("SMTP_PORT", "587")
)

SMTP_USER = os.getenv(
    "SMTP_USER",
    EMAIL_FROM
)

SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]

DRY_RUN = (
    os.getenv("DRY_RUN", "true").lower()
    == "true"
)

# Monday=0
# Friday=4
# Saturday=5
# Sunday=6
BADMINTON_DAYS = {0, 4, 5, 6}


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)


# ============================================================
# Helpers
# ============================================================

def visible_text(page):
    try:
        return page.locator(
            "body"
        ).inner_text(timeout=5000)
    except Exception:
        return ""


def click_text(page, patterns, timeout=5000):
    for pat in patterns:
        try:
            loc = (
                page
                .get_by_text(
                    re.compile(pat, re.I)
                )
                .filter(visible=True)
                .first
            )

            if loc.count():
                loc.click(timeout=timeout)
                return True

        except Exception:
            pass

    return False


def fill_first(page, selectors, value):
    for sel in selectors:
        try:
            loc = (
                page
                .locator(sel)
                .filter(visible=True)
                .first
            )

            if loc.count():
                loc.fill(value)
                return True

        except Exception:
            pass

    return False


# ============================================================
# Browser installation
# ============================================================

def install_browser():
    marker = "/tmp/.mega_chromium_ready"

    if os.path.exists(marker):
        return

    logging.info(
        "Installing Playwright Chromium (first run only)..."
    )

    try:
        subprocess.check_call([
            sys.executable,
            "-m",
            "playwright",
            "install",
            "--with-deps",
            "chromium"
        ])

    except Exception:
        subprocess.check_call([
            sys.executable,
            "-m",
            "playwright",
            "install",
            "chromium"
        ])

    open(marker, "w").close()


# ============================================================
# Login
# ============================================================

def login(page):
    logging.info("Opening Mega Club SimplyBook...")

    page.goto(
        URL,
        wait_until="domcontentloaded",
        timeout=60000
    )

    page.wait_for_timeout(3000)

    click_text(
        page,
        [
            r"accept",
            r"agree",
            r"got it"
        ],
        timeout=2500
    )

    body = visible_text(page)

    if re.search(
        r"log\s*out|sign\s*out|my\s+account|my\s+bookings",
        body,
        re.I
    ):
        logging.info(
            "Existing logged-in session detected."
        )
        return

    click_text(
        page,
        [
            r"sign\s*in",
            r"log\s*in",
            r"login",
            r"my\s+account"
        ],
        timeout=7000
    )

    page.wait_for_timeout(1500)

    user_ok = fill_first(
        page,
        [
            'input[type="email"]',
            'input[name*="login" i]',
            'input[name*="email" i]',
            'input[placeholder*="email" i]',
            'input[placeholder*="login" i]',
            'input[type="text"]',
        ],
        LOGIN
    )

    pass_ok = fill_first(
        page,
        [
            'input[type="password"]',
            'input[name*="password" i]',
            'input[placeholder*="password" i]',
        ],
        PASSWORD
    )

    if not (user_ok and pass_ok):
        page.screenshot(
            path="/tmp/mega_login_fields.png",
            full_page=True
        )

        raise RuntimeError(
            "Could not find the SimplyBook client login fields. "
            "Screenshot saved to /tmp/mega_login_fields.png"
        )

    if not click_text(
        page,
        [
            r"sign\s*in",
            r"log\s*in",
            r"login",
            r"continue"
        ],
        timeout=7000
    ):
        try:
            page.locator(
                "form"
            ).filter(
                visible=True
            ).first.press("Enter")

        except Exception:
            raise RuntimeError(
                "Could not submit SimplyBook login form."
            )

    page.wait_for_timeout(3000)

    body = visible_text(page)

    if re.search(
        r"incorrect|invalid|wrong password|not found",
        body,
        re.I
    ):
        raise RuntimeError(
            "SimplyBook rejected the login credentials."
        )

    logging.info(
        "Client login submitted successfully."
    )


# ============================================================
# Booking time rules
# ============================================================

def target_window(d):
    # Monday-Friday: 19:00-22:00
    if d.weekday() < 5:
        return ("19:00", "22:00")

    # Saturday-Sunday: 12:00-22:00
    return ("12:00", "22:00")


def time_minutes(s):
    m = re.search(
        r"\b(\d{1,2}):(\d{2})\b",
        s
    )

    if not m:
        return None

    return (
        int(m.group(1)) * 60
        + int(m.group(2))
    )


# ============================================================
# BOOK NOW
# ============================================================

def open_book_now(page):
    """
    The logged-in PWA opens on Dashboard.

    We must click BOOK NOW before looking for
    Badminton Court / Tennis Court.

    This project uses Playwright SYNC API.
    No async / await is used.
    """

    body = visible_text(page)

    # Already on Service step
    if re.search(
        r"Step\s*1\s*of\s*3|^\s*Service\s*$",
        body,
        re.I | re.M
    ):
        logging.info(
            "Already on booking service page."
        )
        return True

    logging.info(
        "Opening BOOK NOW from Mega Club dashboard..."
    )

    selectors = [
        "text=BOOK NOW",
        "text=Book Now",
        "button:has-text('BOOK NOW')",
        "button:has-text('Book Now')",
        "a:has-text('BOOK NOW')",
        "a:has-text('Book Now')",
        "[role='button']:has-text('BOOK NOW')",
        "[role='button']:has-text('Book Now')",
    ]

    # Direct selectors
    for selector in selectors:
        try:
            locator = page.locator(
                selector
            ).first

            if locator.count() == 0:
                continue

            if not locator.is_visible():
                continue

            locator.scroll_into_view_if_needed(
                timeout=3000
            )

            locator.click(
                timeout=5000
            )

            logging.info(
                "BOOK NOW clicked successfully: %s",
                selector
            )

            page.wait_for_timeout(2500)

            return True

        except Exception as e:
            logging.debug(
                "BOOK NOW selector failed: %s | %s",
                selector,
                e
            )

    # Fallback: inspect buttons / links
    elements = page.locator(
        "button, a, [role='button']"
    )

    try:
        count = elements.count()

        for i in range(count):
            try:
                element = elements.nth(i)

                if not element.is_visible():
                    continue

                text = (
                    element
                    .inner_text()
                    .strip()
                    .upper()
                )

                if "BOOK NOW" in text:
                    element.scroll_into_view_if_needed(
                        timeout=3000
                    )

                    element.click(
                        timeout=5000
                    )

                    logging.info(
                        "BOOK NOW clicked using fallback text search."
                    )

                    page.wait_for_timeout(2500)

                    return True

            except Exception:
                continue

    except Exception:
        pass

    try:
        page.screenshot(
            path="/tmp/mega_dashboard.png",
            full_page=True
        )
    except Exception:
        pass

    raise RuntimeError(
        "Could not find BOOK NOW on the Mega Club dashboard."
    )


# ============================================================
# Select service
# ============================================================

def choose_service(page, sport):

    service_name = (
        "Badminton Court"
        if sport == "Badminton"
        else "Tennis Court"
    )

    logging.info(
        "Looking for exact service: %s",
        service_name
    )

    try:
        page.get_by_text(
            service_name,
            exact=True
        ).first.wait_for(
            state="visible",
            timeout=10000
        )

    except Exception:
        pass

    locators = [
        page.get_by_text(
            service_name,
            exact=True
        ).first,

        page.locator(
            f'text="{service_name}"'
        ).first,

        page.locator(
            "body"
        ).get_by_text(
            service_name,
            exact=True
        ).first,
    ]

    for loc in locators:

        try:

            if not loc.is_visible():
                continue

            loc.scroll_into_view_if_needed(
                timeout=3000
            )

            # Direct click
            try:
                loc.click(
                    timeout=4000
                )

                page.wait_for_timeout(
                    1200
                )

                logging.info(
                    "Selected service: %s",
                    service_name
                )

                return True

            except Exception:
                pass

            # Try parent/card
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

                    parent = (
                        loc
                        .locator(xp)
                        .first
                    )

                    if (
                        parent.count()
                        and parent.is_visible()
                    ):

                        parent.scroll_into_view_if_needed(
                            timeout=2000
                        )

                        parent.click(
                            timeout=4000
                        )

                        page.wait_for_timeout(
                            1200
                        )

                        logging.info(
                            "Selected service card: %s",
                            service_name
                        )

                        return True

                except Exception:
                    pass

        except Exception:
            pass

    body = visible_text(page)

    if service_name.lower() in body.lower():

        logging.info(
            "Service text IS present in page body "
            "but could not be clicked: %s",
            service_name
        )

    else:

        logging.info(
            "Service text is NOT present in current page body: %s",
            service_name
        )

    try:
        page.screenshot(
            path=f"/tmp/mega_service_{sport.lower()}.png",
            full_page=True
        )
    except Exception:
        pass

    return False


# ============================================================
# Continue / Next / Confirm
# ============================================================

def continue_or_confirm(page):

    patterns = [
        r"continue",
        r"next",
        r"book",
        r"reserve",
        r"confirm",
        r"make\s+booking"
    ]

    if click_text(
        page,
        patterns,
        timeout=2500
    ):

        page.wait_for_timeout(
            1200
        )

        return True

    return False


# ============================================================
# Select date
# ============================================================

def select_date(page, d):

    # Direct date input
    for sel in [
        'input[type="date"]',
        'input[placeholder*="date" i]'
    ]:

        try:

            loc = (
                page
                .locator(sel)
                .filter(visible=True)
                .first
            )

            if loc.count():

                loc.fill(
                    d.isoformat()
                )

                loc.press("Enter")

                page.wait_for_timeout(
                    1500
                )

                logging.info(
                    "Selected date using date input: %s",
                    d.isoformat()
                )

                return

        except Exception:
            pass

    # Calendar
    target_label = d.strftime(
        "%B %Y"
    )

    for _ in range(15):

        body = visible_text(page)

        if target_label.lower() in body.lower():
            break

        clicked = False

        for pat in [
            r"next",
            r"next month",
            r"›",
            r">"
        ]:

            try:

                loc = (
                    page
                    .get_by_text(
                        re.compile(
                            pat,
                            re.I
                        )
                    )
                    .filter(visible=True)
                    .last
                )

                if loc.count():

                    loc.click(
                        timeout=1500
                    )

                    page.wait_for_timeout(
                        400
                    )

                    clicked = True
                    break

            except Exception:
                pass

        if not clicked:

            for attr in [
                "[aria-label*='next' i]",
                "[title*='next' i]"
            ]:

                try:

                    loc = (
                        page
                        .locator(attr)
                        .filter(visible=True)
                        .first
                    )

                    if loc.count():

                        loc.click(
                            timeout=1500
                        )

                        page.wait_for_timeout(
                            400
                        )

                        clicked = True
                        break

                except Exception:
                    pass

        if not clicked:
            break

    day = str(d.day)

    candidates = []

    for sel in [
        "button",
        "[role='button']",
        "td",
        "a"
    ]:

        try:

            for el in (
                page
                .locator(sel)
                .filter(visible=True)
                .all()
            ):

                txt = (
                    el
                    .inner_text()
                    .strip()
                )

                aria = (
                    el.get_attribute(
                        "aria-label"
                    )
                    or ""
                )

                title = (
                    el.get_attribute(
                        "title"
                    )
                    or ""
                )

                combined = (
                    f"{txt} {aria} {title}"
                )

                if (
                    re.fullmatch(
                        day,
                        txt
                    )
                    or
                    re.search(
                        rf"\b{re.escape(d.strftime('%B'))}\s+{day}\b",
                        combined,
                        re.I
                    )
                ):
                    candidates.append(el)

        except Exception:
            pass

    for el in candidates:

        try:

            el.click(
                timeout=2500
            )

            page.wait_for_timeout(
                1200
            )

            logging.info(
                "Selected release date: %s",
                d.isoformat()
            )

            return

        except Exception:
            continue

    raise RuntimeError(
        f"Could not select release date {d.isoformat()}"
    )


# ============================================================
# Choose available time
# ============================================================

def choose_time_from_page(page, d):

    lo_s, hi_s = target_window(d)

    lo = time_minutes(lo_s)
    hi = time_minutes(hi_s)

    candidates = []

    for sel in [
        "button",
        "[role='button']",
        "a"
    ]:

        try:

            for el in (
                page
                .locator(sel)
                .filter(visible=True)
                .all()
            ):

                txt = (
                    el
                    .inner_text()
                    .strip()
                )

                tm = time_minutes(txt)

                if (
                    tm is not None
                    and lo <= tm <= hi
                ):
                    candidates.append(
                        (
                            tm,
                            el,
                            txt
                        )
                    )

        except Exception:
            pass

    seen = set()

    for tm, el, txt in sorted(
        candidates,
        key=lambda x: x[0]
    ):

        if tm in seen:
            continue

        seen.add(tm)

        try:

            el.click(
                timeout=3000
            )

            page.wait_for_timeout(
                1200
            )

            logging.info(
                "Selected time: %s",
                txt
            )

            return txt

        except Exception:
            continue

    return None


# ============================================================
# Required Mega Club acknowledgements
# ============================================================

def agree_rules(page):

    labels = [

        r"I\s+understand\s+the\s+rules\s+and\s+regulations\s+for\s+my\s+booking",

        r"GUEST\s+Acknowledgment\s+Of\s+Risk\s+and\s+Waiver\s+of\s+Liability\s+Use\s+of\s+MEGA\s+CLUB",

        r"I\s+agree\s+with\s+Mega\s+Club\s+Shared\s+Facility\s+Terms\s*&\s*Conditions",
    ]

    checked_count = 0

    for pattern in labels:

        try:

            text_loc = (
                page
                .get_by_text(
                    re.compile(
                        pattern,
                        re.I
                    )
                )
                .filter(visible=True)
                .first
            )

            if (
                not text_loc.count()
                or not text_loc.is_visible()
            ):

                logging.info(
                    "Required confirmation text not found: %s",
                    pattern
                )

                continue

            text_loc.scroll_into_view_if_needed(
                timeout=3000
            )

            clicked = False

            for xp in [
                "xpath=ancestor::label[1]",
                "xpath=ancestor::*[@role='checkbox'][1]",
                "xpath=ancestor::*[.//input[@type='checkbox']][1]",
            ]:

                try:

                    container = (
                        text_loc
                        .locator(xp)
                        .first
                    )

                    if (
                        container.count()
                        and container.is_visible()
                    ):

                        cb = (
                            container
                            .locator(
                                "input[type='checkbox'], "
                                "[role='checkbox']"
                            )
                            .first
                        )

                        if cb.count():

                            try:

                                if cb.is_checked():

                                    checked_count += 1
                                    clicked = True
                                    break

                            except Exception:
                                pass

                        container.click(
                            timeout=3000
                        )

                        page.wait_for_timeout(
                            300
                        )

                        clicked = True
                        checked_count += 1

                        break

                except Exception:
                    pass

            if clicked:
                continue

            text_loc.click(
                timeout=3000
            )

            page.wait_for_timeout(
                300
            )

            checked_count += 1

        except Exception:

            logging.exception(
                "Could not handle confirmation checkbox: %s",
                pattern
            )

    # Safety fallback
    try:

        boxes = (
            page
            .locator(
                "input[type='checkbox'], "
                "[role='checkbox']"
            )
            .filter(visible=True)
        )

        for i in range(
            boxes.count()
        ):

            cb = boxes.nth(i)

            try:

                if not cb.is_checked():
                    cb.check(
                        force=True
                    )

            except Exception:

                try:
                    cb.click(
                        force=True
                    )
                except Exception:
                    pass

    except Exception:
        pass

    logging.info(
        "Processed Mega Club confirmation acknowledgements "
        "(3 required)."
    )

    return checked_count > 0


# ============================================================
# Book one sport
# ============================================================

def book_sport(page, sport, d):

    logging.info(
        "Trying %s for %s",
        sport,
        d
    )

    # Dashboard -> BOOK NOW -> Service
    open_book_now(page)

    # Select Badminton / Tennis
    if not choose_service(
        page,
        sport
    ):

        logging.info(
            "%s service not found on page.",
            sport
        )

        return False

    # Select release date
    select_date(
        page,
        d
    )

    # Select time
    chosen = choose_time_from_page(
        page,
        d
    )

    if not chosen:

        logging.info(
            "No %s slot in target window.",
            sport
        )

        return False

    logging.info(
        "%s candidate selected: %s",
        sport,
        chosen
    )

    # Test mode
    if DRY_RUN:

        logging.info(
            "DRY_RUN=true — stopping before final booking."
        )

        return True

    # Continue to confirmation
    continue_or_confirm(page)

    page.wait_for_timeout(
        800
    )

    # All 3 required acknowledgements
    agree_rules(page)

    continue_or_confirm(page)

    page.wait_for_timeout(
        1200
    )

    # Final confirmation
    if click_text(
        page,
        [
            r"confirm\s+booking",
            r"confirm",
            r"book\s+now",
            r"reserve"
        ],
        timeout=4000
    ):

        page.wait_for_timeout(
            1800
        )

    # Verify booking
    body = visible_text(page)

    if re.search(
        r"booking\s+(confirmed|successful)"
        r"|successfully\s+booked"
        r"|thank\s+you",
        body,
        re.I
    ):

        logging.info(
            "BOOKING SUCCESS: %s %s %s",
            sport,
            d,
            chosen
        )

        return chosen

    if re.search(
        r"error"
        r"|failed"
        r"|not available"
        r"|already booked",
        body,
        re.I
    ):

        raise RuntimeError(
            "Booking did not confirm. "
            "Page reported an error or unavailable state."
        )

    try:

        page.screenshot(
            path="/tmp/mega_after_booking.png",
            full_page=True
        )

    except Exception:
        pass

    raise RuntimeError(
        "Could not verify final booking confirmation; "
        "screenshot saved to /tmp/mega_after_booking.png"
    )


# ============================================================
# Success email
# ============================================================

def send_email(
    sport,
    d,
    chosen
):

    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()

    msg["Subject"] = (
        f"✅ Mega Club booked — "
        f"{sport} {d.isoformat()} "
        f"{str(chosen)[:5]}"
    )

    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    msg.set_content(
        "Your Mega Club booking succeeded.\n\n"
        f"Sport: {sport}\n"
        f"Date: {d.isoformat()}\n"
        f"Start: {str(chosen)[:5]}\n"
    )

    with smtplib.SMTP(
        SMTP_HOST,
        SMTP_PORT,
        timeout=30
    ) as smtp:

        smtp.starttls()

        smtp.login(
            SMTP_USER,
            SMTP_PASSWORD
        )

        smtp.send_message(msg)

    logging.info(
        "Success email sent to %s",
        EMAIL_TO
    )


# ============================================================
# Main
# ============================================================

def main():

    # Optional manual date override
    override = os.getenv(
        "RELEASE_DATE"
    )

    if override:

        d = date.fromisoformat(
            override
        )

    else:

        # Today + 7 days
        d = (
            datetime
            .now(TZ)
            .date()
            + timedelta(days=7)
        )

    logging.info(
        "Target release date: %s",
        d
    )

    # Badminton:
    # Monday / Friday / Saturday / Sunday
    #
    # Priority:
    # Badminton first
    # Tennis second
    #
    # Other days:
    # Tennis only

    if d.weekday() in BADMINTON_DAYS:

        order = [
            "Badminton",
            "Tennis"
        ]

    else:

        order = [
            "Tennis"
        ]

    logging.info(
        "Booking priority: %s",
        " -> ".join(order)
    )

    install_browser()

    with sync_playwright() as pw:

        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox"
            ]
        )

        context = browser.new_context(
            viewport={
                "width": 1440,
                "height": 1100
            }
        )

        page = context.new_page()

        try:

            # Login
            login(page)

            # Try sports in priority order
            for sport in order:

                try:

                    result = book_sport(
                        page,
                        sport,
                        d
                    )

                    # Successful booking / dry-run candidate
                    if result:

                        if not DRY_RUN:

                            send_email(
                                sport,
                                d,
                                result
                            )

                        logging.info(
                            "Finished after successful "
                            "%s selection.",
                            sport
                        )

                        return

                except Exception:

                    logging.exception(
                        "%s attempt failed.",
                        sport
                    )

                    # Reset page before next sport
                    try:

                        page.goto(
                            URL,
                            wait_until="domcontentloaded",
                            timeout=60000
                        )

                        page.wait_for_timeout(
                            1500
                        )

                        login(page)

                    except Exception:

                        logging.exception(
                            "Could not reset page "
                            "before next sport."
                        )

            logging.info(
                "No suitable booking found for %s.",
                d
            )

        finally:

            context.close()
            browser.close()


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    main()
