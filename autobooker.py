import os, json, time, smtplib, logging
from datetime import datetime, date, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo
from pathlib import Path
import requests

TZ = ZoneInfo(os.getenv("TIMEZONE", "America/Toronto"))
COMPANY = os.environ["SIMPLYBOOK_COMPANY"]
LOGIN = os.environ["SIMPLYBOOK_LOGIN"]
PASSWORD = os.environ["SIMPLYBOOK_PASSWORD"]
EMAIL_TO = os.environ["EMAIL_TO"]
EMAIL_FROM = os.getenv("EMAIL_FROM", EMAIL_TO)
SMTP_HOST = os.environ["SMTP_HOST"]
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# Sport/service matching is intentionally name-based because IDs can differ by company.
BADMINTON_NAMES = ("badminton",)
TENNIS_NAMES = ("tennis",)

API = "https://user-api.simplybook.me"
session = requests.Session()
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")

def rpc(url, method, params, token=None):
    headers = {"Content-Type":"application/json"}
    if token:
        headers.update({"X-Company-Login": COMPANY, "X-Token": token})
    payload = {"jsonrpc":"2.0", "method":method, "params":params, "id":int(time.time()*1000)}
    r = session.post(url, json=payload, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"{method}: {data['error']}")
    return data["result"]

def get_token():
    # Official SimplyBook method: getUserToken(companyLogin, userLogin, userPassword).
    return rpc(API + "/login", "getUserToken", [COMPANY, LOGIN, PASSWORD])

def call(method, params, token):
    return rpc(API + "/", method, params, token)

def services(token):
    # getEventList returns services/events for the company.
    result = call("getEventList", [True], token)
    return result if isinstance(result, list) else list(result.values()) if isinstance(result, dict) else []

def name_of(x):
    if not isinstance(x, dict): return ""
    return str(x.get("name") or x.get("event_name") or x.get("title") or "").lower()

def service_id(x):
    if not isinstance(x, dict): return None
    return x.get("id") or x.get("event_id")

def choose_service(items, words):
    for x in items:
        n = name_of(x)
        if any(w in n for w in words):
            return x
    return None

def target_window(d):
    # Weekdays 0-4: 19:00-22:00. Weekend: 12:00-22:00.
    start = "19:00:00" if d.weekday() < 5 else "12:00:00"
    return start, "22:00:00"

def eligible_time(t, start, end):
    return start <= t <= end

def find_slots(token, svc, d):
    sid = service_id(svc)
    if sid is None:
        raise RuntimeError(f"Could not determine service ID from {svc}")
    start, end = target_window(d)
    # Official signature: getStartTimeMatrix(from,to,eventId,unitId,count,...)
    # null unit asks SimplyBook for any suitable unit/provider.
    matrix = call("getStartTimeMatrix", [d.isoformat(), d.isoformat(), sid, None, 1], token)
    times = matrix.get(d.isoformat(), []) if isinstance(matrix, dict) else []
    return [t for t in times if eligible_time(str(t), start, end)]

def load_state():
    p = Path(os.getenv("STATE_FILE","state.json"))
    if not p.exists(): return {}
    try: return json.loads(p.read_text())
    except Exception: return {}

def save_state(s):
    Path(os.getenv("STATE_FILE","state.json")).write_text(json.dumps(s, indent=2))

def already_booked_today():
    s = load_state()
    return s.get("last_success_date")

def book(token, svc, d, t):
    sid = service_id(svc)
    # The exact client-data/additional-field requirements can vary by company.
    client = {
        "name": os.environ.get("CLIENT_NAME", LOGIN.split("@")[0]),
        "email": EMAIL_TO,
        "phone": os.environ.get("CLIENT_PHONE", "")
    }
    # Official book signature shown in SimplyBook docs.
    return call("book", [sid, None, d.isoformat(), t, client, {}, 1], token)

def send_email(subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(msg)

def run_for(d):
    if already_booked_today() == d.isoformat():
        logging.info("State says a booking was already made for %s; stopping.", d)
        return

    token = get_token()
    items = services(token)
    bad = choose_service(items, BADMINTON_NAMES)
    ten = choose_service(items, TENNIS_NAMES)

    candidates = []
    if d.weekday() in (0, 4, 5, 6) and bad:
        candidates.append(("Badminton", bad))
    if ten:
        candidates.append(("Tennis", ten))

    for sport, svc in candidates:
        try:
            slots = find_slots(token, svc, d)
        except Exception as e:
            logging.exception("%s slot lookup failed", sport)
            continue

        # Earliest suitable slot wins. This is deliberately deterministic.
        for t in sorted(map(str, slots)):
            logging.info("%s candidate: %s %s", sport, d, t)
            if DRY_RUN:
                logging.info("DRY_RUN=true: no booking submitted.")
                return

            result = book(token, svc, d, t)
            logging.info("Booking result: %s", result)
            load = load_state()
            load["last_success_date"] = d.isoformat()
            load["last_success_sport"] = sport
            load["last_success_time"] = t
            save_state(load)
            send_email(
                f"✅ Mega Club booked — {sport} {d.isoformat()} {t[:5]}",
                f"Your Mega Club booking succeeded.\n\nSport: {sport}\nDate: {d.isoformat()}\nStart: {t[:5]}\n\nThis system stops after the first successful booking."
            )
            return

    logging.info("No suitable booking found for %s.", d)

if __name__ == "__main__":
    # Default behavior: inspect the next calendar day.
    # In production, set RELEASE_DATE explicitly if Mega Club's release rule differs.
    override = os.getenv("RELEASE_DATE")
    d = date.fromisoformat(override) if override else (datetime.now(TZ).date() + timedelta(days=7))
    run_for(d)
