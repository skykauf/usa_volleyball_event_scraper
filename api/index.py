import html
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from typing import Iterable

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from flask import Flask, jsonify, redirect, render_template_string, request, url_for

USAV_EVENTS_URL = "https://usavolleyball.org/beach-national-team/event-registration/"
VIS_BASE_URL = "https://www.fivb.org/Vis2009/XmlRequest.asmx"
VIS_WORLD_TOUR_FIELDS = (
    "Position TeamName TeamFederationCode EarnedPointsTeam NoPlayer1 NoPlayer2"
)


@dataclass(frozen=True)
class EventDeadline:
    event_name: str
    event_dates_raw: str
    registration_deadline: datetime
    source_url: str = USAV_EVENTS_URL


app = Flask(__name__)
KV_EMAIL_SET_KEY = "usav:reminder_emails"
_VIS_CACHE: dict[str, object] = {
    "expires_at": datetime(1970, 1, 1, tzinfo=UTC),
    "rows": [],
}


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
    # Treat REMINDER_EMAILS as a "seed" list only when KV is empty.
    if not combined:
        combined.update(from_env)
    return sorted(set(combined))


def add_email_to_distribution_list(email: str) -> None:
    normalized = parseaddr(email.strip())[1]
    if not normalized:
        raise RuntimeError("Please provide a valid email address.")
    kv_request(["SADD", KV_EMAIL_SET_KEY, normalized])


def delete_email_from_distribution_list(email: str) -> int:
    normalized = parseaddr(email.strip())[1]
    if not normalized:
        raise RuntimeError("Please provide a valid email address.")
    removed = kv_request(["SREM", KV_EMAIL_SET_KEY, normalized])
    if removed is None:
        return 0
    if isinstance(removed, (int, float)):
        return int(removed)
    # Upstash typically returns an integer, but be defensive.
    try:
        return int(str(removed))
    except Exception:
        return 0


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


def _escape_attr(v: object) -> str:
    return (
        str(v)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_vis_request_xml(request_type: str, attrs: dict[str, object]) -> str:
    parts = [f'<Request Type="{_escape_attr(request_type)}"']
    for key, value in attrs.items():
        if value is None or value == "":
            continue
        parts.append(f' {key}="{_escape_attr(value)}"')
    parts.append(" />")
    # Old-style wrapper works reliably for these ranking endpoints.
    return "<Requests>" + "".join(parts) + "</Requests>"


def _xml_to_records(xml_text: str, node_tag: str) -> list[dict[str, object]]:
    root = ET.fromstring(xml_text)
    records: list[dict[str, object]] = []
    for node in root.findall(f".//{node_tag}"):
        rec: dict[str, object] = {}
        if node.attrib:
            rec.update(node.attrib)
        for child in node:
            if len(child) == 0 and child.text is not None:
                rec[child.tag] = child.text.strip()
            elif child.attrib:
                rec[child.tag] = child.attrib
        records.append(rec)
    return records


def vis_request_xml(
    request_type: str, node_tag: str, attrs: dict[str, object]
) -> list[dict[str, object]]:
    body = _build_vis_request_xml(request_type, attrs)
    resp = requests.post(
        VIS_BASE_URL,
        data=body.encode("utf-8"),
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; USAV-Event-Scraper/1.0)",
            "Content-Type": "application/xml; charset=utf-8",
            "Accept": "application/xml",
        },
        timeout=30,
    )
    resp.raise_for_status()
    text = resp.text or ""
    if not text.strip():
        return []
    try:
        return _xml_to_records(text, node_tag)
    except ET.ParseError:
        return []


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def vis_cache_ttl_minutes() -> int:
    raw = os.getenv("VIS_CACHE_TTL_MINUTES", "30").strip()
    try:
        v = int(raw)
    except ValueError:
        return 30
    return max(1, min(v, 240))


def fetch_usa_world_tour_rankings() -> list[dict[str, object]]:
    now = datetime.now(UTC)
    expires_at = _VIS_CACHE.get("expires_at")
    if isinstance(expires_at, datetime) and now < expires_at:
        rows = _VIS_CACHE.get("rows")
        if isinstance(rows, list):
            return rows

    rows: list[dict[str, object]] = []
    for gender in ("M", "W"):
        records = vis_request_xml(
            "GetBeachWorldTourRanking",
            "BeachWorldTourRankingEntry",
            {
                "Gender": gender,
                "Fields": VIS_WORLD_TOUR_FIELDS,
            },
        )
        for rec in records:
            if str(rec.get("TeamFederationCode", "")).upper() != "USA":
                continue
            rows.append(
                {
                    "gender": gender,
                    "position": _int_or_none(rec.get("Position")),
                    "earned_points": _int_or_none(rec.get("EarnedPointsTeam")),
                    "team_name": rec.get("TeamName"),
                    "no_player1": _int_or_none(rec.get("NoPlayer1")),
                    "no_player2": _int_or_none(rec.get("NoPlayer2")),
                }
            )

    rows.sort(
        key=lambda r: (
            -(r["earned_points"] if isinstance(r["earned_points"], int) else -1),
            (r["position"] if isinstance(r["position"], int) else 10**9),
        )
    )
    _VIS_CACHE["rows"] = rows
    _VIS_CACHE["expires_at"] = now + timedelta(minutes=vis_cache_ttl_minutes())
    return rows


def parse_event_deadlines(page_text: str) -> list[EventDeadline]:
    lines = [line.strip() for line in page_text.splitlines() if line.strip()]
    events: list[EventDeadline] = []
    now = datetime.now(UTC)
    # Lines between title and "Registration Deadline:" for NORCECA-style blocks.
    meta_prefixes = ("Date:", "Location:", "NORCECA Events:")

    for idx, line in enumerate(lines):
        if not line.startswith("Registration Deadline:"):
            continue
        if idx < 1:
            continue

        deadline_label = line.split("Registration Deadline:", 1)[1].strip()
        line_above_deadline = lines[idx - 1]

        # FIVB: "Event name" / "May 13-17, 2026" / "Registration Deadline: March 31"
        # NORCECA: "NORCECA Playoff #2" / "Date: ..." / "Location: ..." / "Registration Deadline: ..."
        if line_above_deadline.startswith(meta_prefixes):
            j = idx - 1
            detail_lines_rev: list[str] = []
            while j >= 0:
                prev = lines[j]
                if prev.startswith(meta_prefixes):
                    detail_lines_rev.append(prev)
                    j -= 1
                    continue
                event_name = prev
                break
            else:
                continue
            event_dates_raw = " | ".join(reversed(detail_lines_rev))
        else:
            if idx < 2:
                continue
            event_name = lines[idx - 2]
            event_dates_raw = line_above_deadline

        year = extract_year(f"{event_dates_raw} {deadline_label}", now.year)

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


def deadline_window_days() -> int:
    """Calendar-day span after `as_of` date: deadline in [today, today+N] inclusive (default N=3)."""
    raw = os.getenv("DEADLINE_WINDOW_DAYS", "3").strip()
    try:
        n = int(raw)
    except ValueError:
        return 3
    return max(0, min(n, 60))


def filter_events_deadline_in_window(
    events: list[EventDeadline],
    as_of: datetime | None = None,
    window_days: int | None = None,
) -> list[EventDeadline]:
    """Registration deadlines from today through today+window_days (UTC dates), inclusive."""
    days = deadline_window_days() if window_days is None else window_days
    when = (as_of or datetime.now(UTC)).date()
    end = when + timedelta(days=days)
    in_window = [e for e in events if when <= e.registration_deadline.date() <= end]
    return sorted(in_window, key=lambda e: e.registration_deadline)


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
    if not events:
        raise RuntimeError("Refusing to send email with zero tournaments in the body.")
    # Default to Resend's onboarding test sender if EMAIL_FROM isn't set.
    # For production, set EMAIL_FROM to a verified sender/domain in Resend.
    from_email = os.getenv("EMAIL_FROM", "USAV Alerts <onboarding@resend.dev>")
    subject = (
        f"USAV registration reminder ({len(events)} tournament(s), "
        f"deadlines in the next {deadline_window_days()} day window)"
    )
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
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        # Show Resend's actual response body to make debugging (403/etc.) easy.
        try:
            details = response.json()
        except Exception:
            details = (response.text or "").strip()
        message = f"Resend error {response.status_code}: {details}"
        raise RuntimeError(message) from None

    try:
        return {"provider": "resend", "response": response.json()}
    except Exception:
        return {"provider": "resend", "response_text": (response.text or "").strip()}


def _preview_html_and_events() -> tuple[str, list[EventDeadline]]:
    """Admin preview + test-send: tournaments whose registration deadline is within the window (UTC)."""
    page_text = fetch_event_page_text()
    events = parse_event_deadlines(page_text)
    n = deadline_window_days()
    in_window = filter_events_deadline_in_window(events)
    if not events:
        return (
            '<p class="muted"><strong>No page data.</strong> Could not parse any events from the source.</p>',
            [],
        )
    if not in_window:
        return (
            '<p class="muted"><strong>Nothing coming up really soon.</strong> There are no registration '
            f"deadlines in the next <strong>{n}</strong> calendar days (today through today+{n}, UTC). "
            "No reminder email would be sent for this window.</p>",
            [],
        )
    intro = (
        f'<p style="margin:0 0 0.75rem;color:#444;">'
        f"{len(in_window)} tournament(s) with upcoming deadlines</p>"
    )
    return intro + format_email_html(in_window), in_window


def get_preview_events() -> list[EventDeadline]:
    _, email_events = _preview_html_and_events()
    return email_events


def latest_event_email_preview() -> str:
    html, _ = _preview_html_and_events()
    return html


@app.get("/")
def home():
    error = request.args.get("error")
    success = request.args.get("success")
    recipients = load_distribution_list()
    preview_html, preview_events = _preview_html_and_events()
    preview_event_count = len(preview_events)
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
          .muted { color: #555; background: #f6f6f6; padding: 0.75rem 1rem; border-radius: 6px; }
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
              <li style="display:flex;align-items:center;justify-content:space-between;gap:1rem;">
                <span>{{ email }}</span>
                <form method="post" action="{{ url_for('delete_email') }}" style="margin:0;">
                  <input type="hidden" name="email" value="{{ email }}" />
                  {% if subscribe_secret_required %}
                    <input type="password" name="subscribe_secret" placeholder="Access code" required />
                  {% endif %}
                  <button type="submit" onclick="return confirm('Remove this email from the list?')">Remove</button>
                </form>
              </li>
            {% endfor %}
          </ul>
        </div>

        <div class="card">
          <h2>Email preview (deadlines in the next {{ deadline_window_days }} days)</h2>
          {{ preview_html|safe }}
          <form id="send-preview-form" method="post" action="{{ url_for('send_preview_email') }}">
            {% if subscribe_secret_required %}
              <input type="password" name="subscribe_secret" placeholder="Access code" required />
            {% endif %}
            <button type="submit" style="margin-top:0.75rem;" {% if preview_event_count == 0 %}disabled title="Nothing in the deadline window to send"{% endif %}>Send this preview</button>
          </form>
          {% if preview_event_count == 0 %}
            <p class="muted" style="margin-top:0.5rem;">Send is disabled when there are no deadlines in the current window.</p>
          {% endif %}
          <div id="send-preview-status" style="margin-top:0.75rem; min-height:1.2em;"></div>
        </div>

        <div class="card">
          <h2>USA player ranking points (VIS, lazy loaded)</h2>
          <div style="display:flex;gap:0.5rem;margin:0.5rem 0;">
            <button type="button" id="usa-toggle-w" style="padding:0.35rem 0.65rem;">Women</button>
            <button type="button" id="usa-toggle-m" style="padding:0.35rem 0.65rem;">Men</button>
            <button type="button" id="usa-toggle-all" style="padding:0.35rem 0.65rem;">All</button>
          </div>
          <p id="usa-rankings-status" class="muted">Loading rankings...</p>
          <div id="usa-rankings-container"></div>
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

          (function() {
            const status = document.getElementById('usa-rankings-status');
            const container = document.getElementById('usa-rankings-container');
            const btnW = document.getElementById('usa-toggle-w');
            const btnM = document.getElementById('usa-toggle-m');
            const btnAll = document.getElementById('usa-toggle-all');
            if (!status || !container || !btnW || !btnM || !btnAll) return;
            let allRows = [];
            let currentFilter = 'W';

            function esc(v) {
              return String(v ?? '')
                .replaceAll('&', '&amp;')
                .replaceAll('<', '&lt;')
                .replaceAll('>', '&gt;')
                .replaceAll('"', '&quot;');
            }

            function filteredRows() {
              if (currentFilter === 'ALL') return allRows;
              return allRows.filter(r => String(r.gender || '').toUpperCase() === currentFilter);
            }

            function paintToggleState() {
              const activeBg = '#111';
              const activeFg = '#fff';
              const inactiveBg = '#fff';
              const inactiveFg = '#111';
              const setState = (btn, active) => {
                btn.style.background = active ? activeBg : inactiveBg;
                btn.style.color = active ? activeFg : inactiveFg;
                btn.style.border = '1px solid #ccc';
              };
              setState(btnW, currentFilter === 'W');
              setState(btnM, currentFilter === 'M');
              setState(btnAll, currentFilter === 'ALL');
            }

            function renderRows() {
              const rows = filteredRows();
              if (!allRows || allRows.length === 0) {
                status.textContent = 'No USA rankings returned from VIS.';
                container.innerHTML = '';
                return;
              }
              if (!rows || rows.length === 0) {
                status.textContent = currentFilter === 'W'
                  ? 'No USA women rankings returned from VIS.'
                  : (currentFilter === 'M' ? 'No USA men rankings returned from VIS.' : 'No USA rankings returned from VIS.');
                container.innerHTML = '';
                return;
              }
              status.textContent = '';
              const header = '<table style="width:100%;border-collapse:collapse;"><thead><tr>' +
                '<th style="text-align:left;border-bottom:1px solid #ddd;padding:6px;">Gender</th>' +
                '<th style="text-align:left;border-bottom:1px solid #ddd;padding:6px;">Position</th>' +
                '<th style="text-align:left;border-bottom:1px solid #ddd;padding:6px;">Points</th>' +
                '<th style="text-align:left;border-bottom:1px solid #ddd;padding:6px;">Team</th>' +
                '</tr></thead><tbody>';
              const body = rows.map(r =>
                '<tr>' +
                `<td style="padding:6px;border-bottom:1px solid #f0f0f0;">${esc(r.gender)}</td>` +
                `<td style="padding:6px;border-bottom:1px solid #f0f0f0;">${esc(r.position ?? '')}</td>` +
                `<td style="padding:6px;border-bottom:1px solid #f0f0f0;">${esc(r.earned_points ?? '')}</td>` +
                `<td style="padding:6px;border-bottom:1px solid #f0f0f0;">${esc(r.team_name ?? '')}</td>` +
                '</tr>'
              ).join('');
              container.innerHTML = header + body + '</tbody></table>';
            }

            function setFilter(nextFilter) {
              currentFilter = nextFilter;
              paintToggleState();
              renderRows();
            }

            btnW.addEventListener('click', () => setFilter('W'));
            btnM.addEventListener('click', () => setFilter('M'));
            btnAll.addEventListener('click', () => setFilter('ALL'));
            paintToggleState();

            fetch('/api/usa-rankings', { headers: { 'Accept': 'application/json' } })
              .then(async (res) => {
                const data = await res.json().catch(() => ({}));
                if (!res.ok || !data.ok) {
                  throw new Error(data.error || 'Failed to load USA rankings.');
                }
                allRows = Array.isArray(data.rows) ? data.rows : [];
                renderRows();
              })
              .catch((err) => {
                status.style.color = '#991b1b';
                status.textContent = 'Failed to load USA rankings: ' + String(err);
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
        preview_event_count=preview_event_count,
        kv_enabled=kv_enabled(),
        subscribe_secret_required=subscribe_secret_required,
        deadline_window_days=deadline_window_days(),
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


@app.post("/delete-email")
def delete_email():
    subscribe_secret = os.getenv("SUBSCRIBE_SECRET")
    if subscribe_secret and request.form.get("subscribe_secret", "") != subscribe_secret:
        return redirect(url_for("home", error="Invalid access code."))
    if not kv_enabled():
        return redirect(url_for("home", error="Redis is not configured yet. Link Upstash in Vercel and redeploy."))

    email = request.form.get("email", "").strip()
    try:
        removed_count = delete_email_from_distribution_list(email)
    except Exception as exc:
        return redirect(url_for("home", error=str(exc)))

    if removed_count <= 0:
        return redirect(url_for("home", error="Email not found in the Redis list."))
    return redirect(url_for("home", success=f"Removed {email}"))


@app.post("/send-preview")
def send_preview_email():
    wants_json = "application/json" in request.headers.get("Accept", "")
    subscribe_secret = os.getenv("SUBSCRIBE_SECRET")
    if subscribe_secret and request.form.get("subscribe_secret", "") != subscribe_secret:
        return redirect(url_for("home", error="Invalid access code."))

    preview_events = get_preview_events()
    if not preview_events:
        msg = (
            f"Nothing to send: no registration deadlines in the next {deadline_window_days()} "
            "calendar days (UTC)."
        )
        if wants_json:
            return jsonify({"ok": False, "error": msg}), 400
        return redirect(url_for("home", error=msg))

    recipients = load_distribution_list()
    if not recipients:
        return redirect(url_for("home", error="Distribution list is empty. Add an email first."))

    try:
        send_email(recipients, preview_events)
    except Exception as exc:
        message = f"Send failed: {exc}"
        if wants_json:
            return jsonify({"ok": False, "error": message}), 500
        return redirect(url_for("home", error=message))

    message = (
        f"Sent preview ({len(preview_events)} in next {deadline_window_days()}-day window) to "
        f"{len(recipients)} recipient(s)."
    )
    if wants_json:
        return jsonify(
            {
                "ok": True,
                "message": message,
                "recipient_count": len(recipients),
                "upcoming_count": len(preview_events),
            }
        )
    return redirect(url_for("home", success=message))


@app.get("/api/health")
def healthcheck():
    return jsonify({"ok": True, "service": "usa_volleyball_event_scraper"})


@app.get("/api/usa-rankings")
def usa_rankings():
    try:
        rows = fetch_usa_world_tour_rankings()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify(
        {
            "ok": True,
            "source": "vis",
            "ranking_type": "beach_world_tour",
            "country_code": "USA",
            "row_count": len(rows),
            "rows": rows,
        }
    )


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
    n = deadline_window_days()
    in_window = filter_events_deadline_in_window(parsed_events, now)

    if not in_window:
        return jsonify(
            {
                "ok": True,
                "sent": False,
                "reason": (
                    f"No registration deadlines in the next {n} calendar days (UTC); "
                    "no email sent."
                ),
                "events_found": len(parsed_events),
                "window_days": n,
                "send_hour_utc": os.getenv("SEND_HOUR_UTC", "").strip()
                or None,
            }
        )

    reminder_triggers = events_requiring_reminder(parsed_events, now)

    if not reminder_triggers:
        return jsonify(
            {
                "ok": True,
                "sent": False,
                "reason": "No events due for reminder at this hour",
                "events_found": len(parsed_events),
                "window_days": n,
                "deadlines_in_window": len(in_window),
                "send_hour_utc": os.getenv("SEND_HOUR_UTC", "").strip()
                or None,
            }
        )

    recipients = load_distribution_list()
    email_result = send_email(recipients, in_window)
    return jsonify(
        {
            "ok": True,
            "sent": True,
            "recipient_count": len(recipients),
            "reminder_triggers": [
                {
                    "event_name": e.event_name,
                    "deadline": e.registration_deadline.date().isoformat(),
                }
                for e in reminder_triggers
            ],
            "upcoming_count_in_email": len(in_window),
            "email_result": email_result,
        }
    )


# Entry point for local dev usage:
#   FLASK_APP=api/index.py flask run
