# OutreachOS v3 — Render Deployment Guide

## What's New in v3
- ✅ **PostgreSQL database** (Render-hosted, persistent)
- ✅ **User accounts** — login/signup with JWT auth
- ✅ **Email verification** — confirmation email on signup
- ✅ **Per-user data** — contacts, categories, settings fully isolated
- ✅ **7-day activity sparkline** — dynamic daily request tracker
- ✅ **Daily log** — tracks requests sent per day vs target, resets each day

---

## Deploy to Render (10 minutes)

### 1. Push to GitHub
```bash
git init
git add .
git commit -m "OutreachOS v3"
git remote add origin https://github.com/YOUR_USERNAME/outreachos.git
git push -u origin main
```

### 2. Create Render PostgreSQL Database
- Go to https://dashboard.render.com
- New → PostgreSQL
- Name: `outreachos-db`
- Plan: Free
- Click Create Database
- **Copy the "Internal Database URL"** for the next step

### 3. Create Render Web Service
- New → Web Service
- Connect your GitHub repo
- Settings:
  - **Root Directory**: *(leave blank)*
  - **Build Command**: `pip install -r backend/requirements.txt`
  - **Start Command**: `cd backend && uvicorn main:app --host 0.0.0.0 --port $PORT`
  - **Plan**: Free

### 4. Set Environment Variables
In the Render web service dashboard → Environment:

| Variable | Value |
|----------|-------|
| `DATABASE_URL` | *(paste Internal Database URL from step 2)* |
| `JWT_SECRET` | *(any long random string, e.g. generate at random.org)* |
| `APP_URL` | `https://your-service-name.onrender.com` |

### 5. Email Verification (Optional but recommended)
For Gmail, create an App Password:
- Google Account → Security → 2-Step Verification → App Passwords
- Create password for "Mail"

| Variable | Value |
|----------|-------|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | `your@gmail.com` |
| `SMTP_PASS` | `your-16-char-app-password` |

> ⚠️ If SMTP is not configured, signup still works — verification emails are skipped (logged to console). To test without email, temporarily set accounts as verified via the DB, or add a dev bypass.

### 6. Deploy
Click "Deploy" — Render installs deps, starts server, creates all DB tables automatically.

---

## Database Schema

All tables are created automatically on first boot via `init_db()`.

| Table | Purpose |
|-------|---------|
| `users` | Accounts, verification tokens, password hashes |
| `contacts` | Outreach contacts (per user) |
| `stage_history` | Full audit log of stage changes |
| `categories` | Custom categories + subcategories (per user) |
| `ai_messages` | Groq-generated drafts (per contact) |
| `chat_history` | AI assistant conversation history (per user) |
| `settings` | User-level settings (Groq key, targets, timing) |
| `daily_log` | Per-user per-day request counts for sparkline |

---

## Daily Target Logic

The dashboard target counter works as follows:
- Each time you **add a new LinkedIn contact**, `daily_log` increments by 1 for today
- The ring and progress bar show `sent_today / daily_target`
- The **7-day sparkline** shows historical activity automatically
- Everything resets at midnight (date-based, no cron needed)
- Target is configurable in Settings

---

## Local Development

```bash
# Install deps
pip install -r backend/requirements.txt

# Set env vars
export DATABASE_URL="postgresql://user:pass@localhost/outreachos"
export JWT_SECRET="dev-secret-change-this"
export APP_URL="http://localhost:8000"

# Run
cd backend
uvicorn main:app --reload --port 8000
```

Then open http://localhost:8000

---

## Troubleshooting

**"Email not verified" on login**
→ Check your spam folder, or configure SMTP properly

**Database connection errors**
→ Make sure DATABASE_URL starts with `postgresql://` not `postgres://` (handled automatically)

**Free tier cold starts**
→ Render free tier spins down after 15min inactivity — first request after idle takes ~30s
→ Consider upgrading to Starter ($7/mo) for always-on

**SMTP errors**
→ Use Gmail App Passwords, not your regular password
→ Make sure 2FA is enabled on your Google account first
