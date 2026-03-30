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

- **When jobs run:** set the time in **`vercel.json`** only (`crons[].schedule`). This repo uses `0 14 * * *` (≈once per day around 14:00 UTC) so it stays within [Hobby cron limits](https://vercel.com/docs/cron-jobs/usage-and-pricing). You do **not** need a matching `SEND_HOUR_UTC` for that.
- **Optional `SEND_HOUR_UTC`:** use only if your cron runs **more than once per day** (e.g. Pro + hourly `0 * * * *`). Then set `SEND_HOUR_UTC` to a single UTC hour (0–23) so emails only send during that hour; leave it **unset** for daily cron.
- The app scrapes event/deadline text from the source page.
- Web UI at `GET /` lets users add addresses to the distribution list.
- Email addresses are persisted in Vercel KV (Redis).
- It sends reminder emails only when:
  - (if `SEND_HOUR_UTC` is set) current UTC hour equals that value
  - event registration deadline is in the future or today
  - days remaining until deadline is divisible by `3`

## Project structure

- `api/index.py` Flask app with admin UI, cron endpoint, scraper, parser, and email sender
- `vercel.json` rewrites (required for Flask on Vercel) + daily cron schedule (Hobby-compatible)
- `requirements.txt` Python dependencies

## Environment variables (Vercel)

Set these in Vercel Project Settings -> Environment Variables:

- `EMAIL_FROM` - verified sender (e.g. `USAV Alerts <alerts@yourdomain.com>`)
- `REMINDER_EMAILS` - comma-separated list of recipients
  - seed/default list if KV is empty or unavailable
- `SEND_HOUR_UTC` - **optional**; only for **hourly** (or frequent) crons — restrict sends to this UTC hour (0–23). Omit for **daily** cron.
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
4. **Production branch:** ensure Vercel is connected to the branch you push (usually `main`) under Project → Settings → Git.
5. **Root directory:** leave blank unless this app lives in a subfolder of a monorepo.
6. Framework: Vercel usually auto-detects **Flask** from `requirements.txt`. **Other** is also fine as long as the repo root contains `api/index.py` and `vercel.json`.
7. Add the environment variables listed above.
8. In Vercel, add Redis/KV via Marketplace (Upstash) and attach it so `KV_REST_API_*` is injected.
9. Deploy (or push a commit; Git integration redeploys automatically).
10. After deploy, verify:
   - `GET /` renders the admin page
   - `GET /api/health` returns `ok: true`
   - In Vercel, go to **Logs** and inspect a cron invocation.

## Troubleshooting Vercel

### Deploy fails: “Hobby accounts are limited to daily cron jobs”

On Hobby, cron expressions that run **more than once per day** are rejected at deploy time. See [Vercel cron usage & pricing](https://vercel.com/docs/cron-jobs/usage-and-pricing). This repo uses a **daily** cron aligned with `SEND_HOUR_UTC`. Do not set `0 * * * *` unless the project is on **Pro**.

### `404` / `DEPLOYMENT_NOT_FOUND` on `*.vercel.app`

That response means **there is no successful production deployment** for that project (or the URL points at a deleted/old deployment). It is not your Flask `404` page.

1. Open the project on [Vercel Dashboard](https://vercel.com/dashboard) → **Deployments**.
2. If the list is empty or every deploy is **Error** / **Canceled**:
   - Confirm the GitHub repo has commits on the branch Vercel uses (e.g. `main`).
   - Open the latest deployment → **Building** / **Runtime Logs** and fix the reported error (missing files, build timeout, etc.).
3. If deploys succeed but the site 404s on `/`:
   - This repo includes a rewrite so all paths go to the Flask app (`vercel.json` → `destination: /api/index`), matching the [official Flask on Vercel example](https://github.com/vercel/examples/tree/main/python/flask3). Ensure you deployed a revision that contains that `vercel.json`.
4. After a green deployment, open the deployment’s **Visit** link, or set **Production Branch** and use the production domain shown under **Settings → Domains**.

## Notes

- Vercel cron schedules use **UTC**. Hobby invocations are “hour bucket” fuzzy (e.g. 14:00–14:59 for `0 14 * * *`); see [docs](https://vercel.com/docs/cron-jobs/usage-and-pricing).
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
