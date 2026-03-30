import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Iterable

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from flask import Flask, jsonify, request

USAV_EVENTS_URL = "https://usavolleyball.org/beach-national-team/event-registration/"


@dataclass(frozen=True)
class EventDeadline:
    event_name: str
    event_dates_raw: str
    registration_deadline: datetime
    source_url: str = USAV_EVENTS_URL


app = Flask(__name__)


def get_env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def parse_recipient_emails(raw: str) -> list[str]:
    emails = []
    for part in raw.split(","):
        email = parseaddr(part.strip())[1]
        if email:
            emails.append(email)
    if not emails:
        raise RuntimeError("No valid recipient emails found in REMINDER_EMAILS")
    return emails


def extract_year(text: str, fallback_year: int) -> int:
    year_match = re.search(r"\b(20\d{2})\b", text)
    if year_match:
        return int(year_match.group(1))
    return fallback_year


def fetch_event_page_text() -> str:
    response = requests.get(USAV_EVENTS_URL, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    return soup.get_text("\n", strip=True)


def parse_event_deadlines(page_text: str) -> list[EventDeadline]:
    lines = [line.strip() for line in page_text.splitlines() if line.strip()]
    events: list[EventDeadline] = []
    now = datetime.now(UTC)

    for idx, line in enumerate(lines):
        if not line.startswith("Registration Deadline:"):
            continue
        if idx < 2:
            continue

        deadline_label = line.split("Registration Deadline:", 1)[1].strip()
        event_dates_raw = lines[idx - 1]
        event_name = lines[idx - 2]
        year = extract_year(event_dates_raw, now.year)

        try:
            parsed_deadline = date_parser.parse(
                f"{deadline_label} {year}", fuzzy=True, dayfirst=False
            )
        except (ValueError, TypeError):
            continue

        deadline_utc = datetime(
            parsed_deadline.year,
            parsed_deadline.month,
            parsed_deadline.day,
            tzinfo=UTC,
        )
        events.append(
            EventDeadline(
                event_name=event_name,
                event_dates_raw=event_dates_raw,
                registration_deadline=deadline_utc,
            )
        )

    deduped = {}
    for event in events:
        key = (event.event_name, event.registration_deadline.date().isoformat())
        deduped[key] = event

    return sorted(deduped.values(), key=lambda event: event.registration_deadline)


def events_requiring_reminder(
    events: Iterable[EventDeadline], now: datetime
) -> list[EventDeadline]:
    target_send_hour = int(os.getenv("SEND_HOUR_UTC", "14"))
    if now.hour != target_send_hour:
        return []

    due: list[EventDeadline] = []
    today = now.date()
    for event in events:
        days_remaining = (event.registration_deadline.date() - today).days
        if days_remaining >= 0 and days_remaining % 3 == 0:
            due.append(event)
    return due


def format_email_html(events: list[EventDeadline]) -> str:
    rows = []
    for event in events:
        rows.append(
            f"<li><strong>{event.event_name}</strong><br>"
            f"Event dates: {event.event_dates_raw}<br>"
            f"Registration deadline: {event.registration_deadline.date().isoformat()}</li>"
        )
    return (
        "<p>USA Volleyball registration reminder:</p>"
        "<ul>"
        + "".join(rows)
        + "</ul>"
        f"<p>Source: <a href='{USAV_EVENTS_URL}'>{USAV_EVENTS_URL}</a></p>"
    )


def send_email(recipients: list[str], events: list[EventDeadline]) -> dict:
    resend_api_key = get_env("RESEND_API_KEY")
    from_email = get_env("EMAIL_FROM")

    payload = {
        "from": from_email,
        "to": recipients,
        "subject": f"USAV registration reminder ({len(events)} events due soon)",
        "html": format_email_html(events),
    }
    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {resend_api_key}",
            "Content-Type": "application/json",
        },
        data=json.dumps(payload),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


@app.get("/api/health")
def healthcheck():
    return jsonify({"ok": True, "service": "usa_volleyball_event_scraper"})


@app.get("/api/cron")
def run_cron():
    auth_secret = os.getenv("CRON_SECRET")
    if auth_secret:
        incoming = request.headers.get("Authorization", "")
        if incoming != f"Bearer {auth_secret}":
            return jsonify({"ok": False, "error": "unauthorized"}), 401

    now = datetime.now(UTC)
    page_text = fetch_event_page_text()
    parsed_events = parse_event_deadlines(page_text)
    reminder_events = events_requiring_reminder(parsed_events, now)

    if not reminder_events:
        return jsonify(
            {
                "ok": True,
                "sent": False,
                "reason": "No events due for reminder at this hour",
                "events_found": len(parsed_events),
                "send_hour_utc": int(os.getenv("SEND_HOUR_UTC", "14")),
            }
        )

    recipients = parse_recipient_emails(
        os.getenv("REMINDER_EMAILS", "skylerkaufman@gmail.com")
    )
    email_result = send_email(recipients, reminder_events)
    return jsonify(
        {
            "ok": True,
            "sent": True,
            "recipient_count": len(recipients),
            "events_reminded": [
                {
                    "event_name": event.event_name,
                    "deadline": event.registration_deadline.date().isoformat(),
                }
                for event in reminder_events
            ],
            "email_result": email_result,
        }
    )


# Entry point for local dev usage:
#   FLASK_APP=api/index.py flask run
