# usa_volleyball_event_scraper

Python web app that scrapes USA Volleyball Beach National Team event registration pages and sends reminder emails every 3 days before each registration deadline.

Source page:
- https://usavolleyball.org/beach-national-team/event-registration/

Vercel links:
- Dashboard: https://vercel.com/dashboard
- New project import: https://vercel.com/new
- Project environment variables docs: https://vercel.com/docs/projects/environment-variables
- Cron jobs docs: https://vercel.com/docs/cron-jobs
- GitHub integration docs: https://vercel.com/docs/deployments/git/vercel-for-github
- Vercel KV docs: https://vercel.com/docs/storage/vercel-kv

## How it works

- Vercel Cron calls `GET /api/cron` hourly.
- The app scrapes event/deadline text from the source page.
- Web UI at `GET /` lets users add addresses to the distribution list.
- Email addresses are persisted in Vercel KV (Redis).
- It sends reminder emails only when:
  - current UTC hour equals `SEND_HOUR_UTC` (default `14`)
  - event registration deadline is in the future or today
  - days remaining until deadline is divisible by `3`
- This avoids sending every hour while still checking hourly.

## Project structure

- `api/index.py` Flask app with admin UI, cron endpoint, scraper, parser, and email sender
- `vercel.json` hourly cron schedule
- `requirements.txt` Python dependencies

## Environment variables (Vercel)

Set these in Vercel Project Settings -> Environment Variables:

- `EMAIL_FROM` - verified sender (e.g. `USAV Alerts <alerts@yourdomain.com>`)
- `REMINDER_EMAILS` - comma-separated list of recipients
  - seed/default list if KV is empty or unavailable
- `SEND_HOUR_UTC` - hour 0-23 when emails can be sent (default `14`)
- `CRON_SECRET` - shared secret for cron endpoint authorization
- `RESEND_API_KEY` - Resend API key
- `KV_REST_API_URL` - Vercel KV REST URL
- `KV_REST_API_TOKEN` - Vercel KV REST token
- `SUBSCRIBE_SECRET` - optional shared password required to submit the signup form

## Resend quick setup (recommended)

1. Create account: https://resend.com
2. Get API key: https://resend.com/api-keys
3. For immediate testing, use:
   - `EMAIL_FROM=USAV Alerts <onboarding@resend.dev>`
   - recipient must be your Resend account email while in test mode
4. For production recipients, add and verify your domain in Resend:
   - https://resend.com/domains
   - then set `EMAIL_FROM` to that verified domain address
5. In Vercel project env vars, set:
   - `RESEND_API_KEY=<your key>`
   - `EMAIL_FROM=<verified sender>`
   - `REMINDER_EMAILS=skylerkaufman@gmail.com`
   - `SEND_HOUR_UTC=14`
   - `CRON_SECRET=<random secret>`
   - `KV_REST_API_URL=<from Vercel KV integration>`
   - `KV_REST_API_TOKEN=<from Vercel KV integration>`

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
export RESEND_API_KEY="re_your_api_key_here"
export EMAIL_FROM="USAV Alerts <onboarding@resend.dev>"
export REMINDER_EMAILS="skylerkaufman@gmail.com"
export SEND_HOUR_UTC=14
export CRON_SECRET=your-secret
export KV_REST_API_URL="..."
export KV_REST_API_TOKEN="..."
FLASK_APP=api/index.py flask run
```

Test:
- `http://127.0.0.1:5000/` admin page
- `http://127.0.0.1:5000/api/health`
- `http://127.0.0.1:5000/api/cron` with header `Authorization: Bearer your-secret`

## Deploy to Vercel (GitHub integration)

1. Push this repo to GitHub.
2. In Vercel, click **Add New... -> Project**.
3. Import the `usa_volleyball_event_scraper` GitHub repo.
4. Framework preset can stay **Other**.
5. Add the environment variables listed above.
6. In Vercel, add a KV database and attach/integrate it with this project.
7. Deploy.
8. After deploy, verify:
   - `GET /` renders the admin page
   - `GET /api/health` returns `ok: true`
   - In Vercel, go to **Storage/Logs** and inspect a cron invocation.

## Notes

- Vercel cron triggers use UTC schedule.
- The parser is text-based and resilient to most layout changes, but large upstream page format changes may require parser updates.
- Email delivery is Resend-only in this project.
- Without `SUBSCRIBE_SECRET`, anyone who can load `/` can add emails to your list. Set it for a simple gate.

## Original build prompt (record)

```text
i'm building a new app called usa_volleyball_event_scraper. its an empty repo locally connected to my github

can you set up a simple pythonic webapp which scrapes the USA Volleyball events website hourly: https://usavolleyball.org/beach-national-team/event-registration/

i want it to send a reminder email to a set of email addresses once every 3 days before an event's registration deadline. to start, the set of emails can be [skylerkaufman@gmail.com]

i want this webapp natively deployed to vercel via github integration

please set this up as programmatically as possible and tell me of any UI actions i need to do to get this online
```
