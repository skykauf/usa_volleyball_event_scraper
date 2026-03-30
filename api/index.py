import html
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
from flask import Flask, jsonify, redirect, render_template_string, request, url_for

USAV_EVENTS_URL = "https://usavolleyball.org/beach-national-team/event-registration/"


@dataclass(frozen=True)
class EventDeadline:
    event_name: str
    event_dates_raw: str
    registration_deadline: datetime
    source_url: str = USAV_EVENTS_URL


app = Flask(__name__)
KV_EMAIL_SET_KEY = "usav:reminder_emails"


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


def kv_rest_credentials() -> tuple[str, str] | None:
    """Vercel Marketplace Upstash may inject UPSTASH_*; legacy KV uses KV_REST_*."""
    url = (os.getenv("KV_REST_API_URL") or os.getenv("UPSTASH_REDIS_REST_URL") or "").strip()
    token = (os.getenv("KV_REST_API_TOKEN") or os.getenv("UPSTASH_REDIS_REST_TOKEN") or "").strip()
    if url and token:
        return url, token
    return None


def kv_enabled() -> bool:
    return kv_rest_credentials() is not None


def kv_request(command: list[str]) -> list | str | int | None:
    creds = kv_rest_credentials()
    if not creds:
        raise RuntimeError(
            "Redis/KV is not configured. Add Upstash Redis from the Vercel Marketplace "
            "and link it to this project (or set KV_REST_API_* / UPSTASH_REDIS_REST_*)."
        )
    url, token = creds
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        data=json.dumps(command),
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise RuntimeError(payload["error"])
    return payload.get("result")


def load_distribution_list() -> list[str]:
    raw_env = os.getenv("REMINDER_EMAILS", "").strip()
    if not raw_env:
        raw_env = "skylerkaufman@gmail.com"
    from_env = parse_recipient_emails(raw_env)
    if not kv_enabled():
        return sorted(set(from_env))

    from_kv = kv_request(["SMEMBERS", KV_EMAIL_SET_KEY]) or []
    combined = {parseaddr(email)[1] for email in from_kv if parseaddr(email)[1]}
    combined.update(from_env)
    return sorted(combined)


def add_email_to_distribution_list(email: str) -> None:
    normalized = parseaddr(email.strip())[1]
    if not normalized:
        raise RuntimeError("Please provide a valid email address.")
    kv_request(["SADD", KV_EMAIL_SET_KEY, normalized])


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
    # Daily cron (Hobby): only `vercel.json` schedule matters — no env needed.
    # Hourly cron (Pro): set SEND_HOUR_UTC=0-23 so only that UTC hour sends.
    raw_hour = os.getenv("SEND_HOUR_UTC", "").strip()
    if raw_hour:
        try:
            target = int(raw_hour)
        except ValueError:
            raise RuntimeError("SEND_HOUR_UTC must be an integer 0-23") from None
        if not 0 <= target <= 23:
            raise RuntimeError("SEND_HOUR_UTC must be between 0 and 23")
        if now.hour != target:
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
            f"<li><strong>{html.escape(event.event_name)}</strong><br>"
            f"Event dates: {html.escape(event.event_dates_raw)}<br>"
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
    # Default to Resend's onboarding test sender if EMAIL_FROM isn't set.
    # For production, set EMAIL_FROM to a verified sender/domain in Resend.
    from_email = os.getenv("EMAIL_FROM", "USAV Alerts <onboarding@resend.dev>")
    subject = f"USAV registration reminder ({len(events)} events due soon)"
    html_body = format_email_html(events)
    resend_api_key = get_env("RESEND_API_KEY")
    payload = {
        "from": from_email,
        "to": recipients,
        "subject": subject,
        "html": html_body,
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
    return {"provider": "resend", "response": response.json()}


def get_preview_events() -> list[EventDeadline]:
    page_text = fetch_event_page_text()
    events = parse_event_deadlines(page_text)
    now = datetime.now(UTC).date()
    upcoming = [event for event in events if event.registration_deadline.date() >= now]
    preview_events = upcoming[:1] if upcoming else events[:1]
    return preview_events


def latest_event_email_preview() -> str:
    preview_events = get_preview_events()
    if not preview_events:
        return "<p>No events found on the source page.</p>"
    return format_email_html(preview_events)


@app.get("/")
def home():
    error = request.args.get("error")
    success = request.args.get("success")
    recipients = load_distribution_list()
    preview_html = latest_event_email_preview()
    subscribe_secret_required = bool(os.getenv("SUBSCRIBE_SECRET"))
    html = """
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>USAV Reminder Admin</title>
        <style>
          body { font-family: Arial, sans-serif; margin: 2rem auto; max-width: 760px; line-height: 1.5; padding: 0 1rem; }
          h1, h2 { margin-bottom: 0.5rem; }
          form { display: flex; gap: 0.5rem; margin: 1rem 0; }
          input[type="email"] { flex: 1; padding: 0.6rem; }
          button { padding: 0.6rem 0.9rem; cursor: pointer; }
          .card { border: 1px solid #ddd; border-radius: 8px; padding: 1rem; margin-bottom: 1rem; }
          .ok { color: #065f46; }
          .err { color: #991b1b; }
        </style>
      </head>
      <body>
        <h1>USA Volleyball Reminder Admin</h1>
        <p>Manage the reminder distribution list and preview the current email content.</p>
        {% if success %}<p class="ok">{{ success }}</p>{% endif %}
        {% if error %}<p class="err">{{ error }}</p>{% endif %}

        <div class="card">
          <h2>Add email address</h2>
          <form method="post" action="{{ url_for('subscribe') }}">
            <input type="email" name="email" placeholder="name@example.com" required />
            {% if subscribe_secret_required %}
              <input type="password" name="subscribe_secret" placeholder="Access code" required />
            {% endif %}
            <button type="submit">Add</button>
          </form>
          {% if not kv_enabled %}
            <p class="err">Redis is not linked. Add Upstash Redis from the Vercel Marketplace and redeploy, or set KV_REST_* / UPSTASH_REDIS_REST_* env vars.</p>
          {% endif %}
        </div>

        <div class="card">
          <h2>Current distribution list</h2>
          <ul>
            {% for email in recipients %}
              <li>{{ email }}</li>
            {% endfor %}
          </ul>
        </div>

        <div class="card">
          <h2>Most recent event email preview</h2>
          {{ preview_html|safe }}
          <form id="send-preview-form" method="post" action="{{ url_for('send_preview_email') }}">
            {% if subscribe_secret_required %}
              <input type="password" name="subscribe_secret" placeholder="Access code" required />
            {% endif %}
            <button type="submit" style="margin-top:0.75rem;">Send this preview</button>
          </form>
          <div id="send-preview-status" style="margin-top:0.75rem; min-height:1.2em;"></div>
        </div>

        <script>
          (function() {
            const form = document.getElementById('send-preview-form');
            const status = document.getElementById('send-preview-status');
            if (!form || !status) return;
            form.addEventListener('submit', async (e) => {
              e.preventDefault();
              const btn = form.querySelector('button[type="submit"]');
              if (btn) btn.disabled = true;
              status.className = '';
              status.style.color = '';
              status.textContent = 'Sending...';
              try {
                const res = await fetch(form.action, {
                  method: 'POST',
                  headers: { 'Accept': 'application/json' },
                  body: new FormData(form)
                });
                const data = await res.json().catch(() => ({}));
                if (res.ok && data.ok) {
                  status.style.color = '#065f46';
                  status.textContent = data.message || 'Sent.';
                } else {
                  status.style.color = '#991b1b';
                  status.textContent = data.error || data.message || 'Send failed.';
                }
              } catch (err) {
                status.style.color = '#991b1b';
                status.textContent = 'Send failed: ' + String(err);
              } finally {
                if (btn) btn.disabled = false;
              }
            });
          })();
        </script>
      </body>
    </html>
    """
    return render_template_string(
        html,
        error=error,
        success=success,
        recipients=recipients,
        preview_html=preview_html,
        kv_enabled=kv_enabled(),
        subscribe_secret_required=subscribe_secret_required,
    )


@app.post("/subscribe")
def subscribe():
    email = request.form.get("email", "").strip()
    subscribe_secret = os.getenv("SUBSCRIBE_SECRET")
    if subscribe_secret and request.form.get("subscribe_secret", "") != subscribe_secret:
        return redirect(url_for("home", error="Invalid access code."))
    if not kv_enabled():
        return redirect(url_for("home", error="Redis is not configured yet. Link Upstash in Vercel and redeploy."))
    try:
        add_email_to_distribution_list(email)
    except Exception as exc:
        return redirect(url_for("home", error=str(exc)))
    return redirect(url_for("home", success=f"Added {email}"))


@app.post("/send-preview")
def send_preview_email():
    subscribe_secret = os.getenv("SUBSCRIBE_SECRET")
    if subscribe_secret and request.form.get("subscribe_secret", "") != subscribe_secret:
        return redirect(url_for("home", error="Invalid access code."))

    preview_events = get_preview_events()
    if not preview_events:
        return redirect(url_for("home", error="No preview events found on the source page."))

    recipients = load_distribution_list()
    if not recipients:
        return redirect(url_for("home", error="Distribution list is empty. Add an email first."))

    wants_json = "application/json" in request.headers.get("Accept", "")

    try:
        send_email(recipients, preview_events)
    except Exception as exc:
        message = f"Send failed: {exc}"
        if wants_json:
            return jsonify({"ok": False, "error": message}), 500
        return redirect(url_for("home", error=message))

    message = f"Sent preview email to {len(recipients)} recipient(s)."
    if wants_json:
        return jsonify({"ok": True, "message": message, "recipient_count": len(recipients)})
    return redirect(url_for("home", success=message))


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
                "send_hour_utc": os.getenv("SEND_HOUR_UTC", "").strip()
                or None,
            }
        )

    recipients = load_distribution_list()
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
