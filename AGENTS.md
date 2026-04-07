# Agent instructions

## Git

When the user explicitly asks to **commit and push** (including to **`main`**), do so in this repository: stage the relevant files, write a clear commit message that summarizes the change, commit, and `git push origin main`. Confirm the working tree is clean afterward.

Do not push to `main` without a direct request to commit/push.

## Project

- **App:** Flask in `api/index.py`, deployed on Vercel (see `vercel.json`).
- **Cron:** `GET /api/cron` (optional `Authorization: Bearer <CRON_SECRET>`).
- **Env / ops:** `README.md` and `.env.example` list variables (Resend, Redis/KV, etc.).
