from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from typing import Optional, List
import asyncpg, json, os, uuid, secrets, smtplib, re
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from groq import Groq
import jwt
import bcrypt
import asyncio
from contextlib import asynccontextmanager

# ─── CONFIG ────────────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

JWT_SECRET = os.environ.get("JWT_SECRET", secrets.token_hex(32))
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24 * 7

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
APP_URL = os.environ.get("APP_URL", "http://localhost:8000")

GROQ_MODEL = "llama-3.3-70b-versatile"

db_pool = None

# ─── LIFESPAN ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    await init_db()
    yield
    await db_pool.close()

app = FastAPI(title="OutreachOS API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

security = HTTPBearer(auto_error=False)

# ─── DATABASE INIT ─────────────────────────────────────────────────────────────
async def init_db():
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_verified BOOLEAN DEFAULT FALSE,
                verification_token TEXT,
                reset_token TEXT,
                reset_token_expires TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS contacts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                company TEXT,
                role TEXT,
                linkedin_url TEXT,
                email TEXT,
                channel TEXT DEFAULT 'linkedin',
                stage TEXT DEFAULT 'request_sent',
                category_id TEXT,
                subcategory TEXT,
                notes TEXT,
                connection_date TIMESTAMPTZ,
                added_date TIMESTAMPTZ DEFAULT NOW(),
                last_action_date TIMESTAMPTZ DEFAULT NOW(),
                is_rejected BOOLEAN DEFAULT FALSE
            );

            CREATE TABLE IF NOT EXISTS stage_history (
                id SERIAL PRIMARY KEY,
                contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
                stage TEXT NOT NULL,
                note TEXT DEFAULT '',
                date TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS categories (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                subcategories JSONB DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS ai_messages (
                id SERIAL PRIMARY KEY,
                contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
                message_type TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS chat_sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                target_type TEXT DEFAULT 'general',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS chat_history (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                session_id TEXT REFERENCES chat_sessions(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS settings (
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (user_id, key)
            );

            CREATE TABLE IF NOT EXISTS daily_log (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                log_date DATE NOT NULL DEFAULT CURRENT_DATE,
                requests_sent INTEGER DEFAULT 0,
                UNIQUE(user_id, log_date)
            );

            CREATE INDEX IF NOT EXISTS idx_contacts_user ON contacts(user_id);
            CREATE INDEX IF NOT EXISTS idx_contacts_stage ON contacts(stage);
            CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_history(user_id);
            CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_history(session_id);
        """)
        # Add connection_date column if it doesn't exist (migration)
        try:
            await conn.execute("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS connection_date TIMESTAMPTZ")
        except:
            pass
        # Add session_id to chat_history if missing (migration)
        try:
            await conn.execute("ALTER TABLE chat_history ADD COLUMN IF NOT EXISTS session_id TEXT REFERENCES chat_sessions(id) ON DELETE CASCADE")
        except:
            pass

# ─── HELPERS ───────────────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())

def create_token(user_id: str, username: str) -> str:
    payload = {
        "sub": user_id,
        "username": username,
        "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRE_HOURS)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(401, "Not authenticated")
    payload = decode_token(credentials.credentials)
    user_id = payload.get("sub")
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE id=$1", user_id)
    if not user:
        raise HTTPException(401, "User not found")
    if not user["is_verified"]:
        raise HTTPException(403, "Please verify your email before continuing")
    return dict(user)

def days_since(dt) -> int:
    if not dt:
        return 0
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except:
            return 0
    if dt.tzinfo:
        from datetime import timezone
        now = datetime.now(timezone.utc)
    else:
        now = datetime.now()
    return max(0, (now - dt).days)

async def get_setting(conn, user_id: str, key: str, default: str = "") -> str:
    row = await conn.fetchrow("SELECT value FROM settings WHERE user_id=$1 AND key=$2", user_id, key)
    return row["value"] if row else default

def compute_playbook_status(contact: dict, settings: dict) -> dict:
    stage = contact.get("stage", "")
    since = days_since(contact.get("last_action_date"))
    channel = contact.get("channel", "linkedin")

    accept_days = int(settings.get("accept_check_days", "3"))
    dm_gap = int(settings.get("dm_gap_days", "2"))
    followup_days = int(settings.get("followup_days", "4"))
    rejection_days = int(settings.get("rejection_recheck_days", "45"))

    if channel == "linkedin":
        if stage == "request_sent":
            if since >= accept_days:
                return {"action": "check_acceptance", "due": True, "urgency": "high",
                        "days_overdue": since - accept_days, "message": f"Check if accepted — sent {since}d ago"}
            return {"action": "waiting_acceptance", "due": False, "days_until": accept_days - since}
        if stage == "request_accepted":
            if since >= dm_gap:
                return {"action": "send_dm", "due": True, "urgency": "high",
                        "days_overdue": since - dm_gap, "message": "Send first DM — they accepted!"}
            return {"action": "waiting_to_dm", "due": False, "days_until": dm_gap - since}
        if stage in ["dm_sent", "followup_sent"]:
            if since >= followup_days:
                urgency = "medium" if since < followup_days * 2 else "high"
                return {"action": "followup", "due": True, "urgency": urgency,
                        "days_overdue": since - followup_days, "message": f"Follow-up due — {since}d since last message"}
            return {"action": "waiting_reply", "due": False, "days_until": followup_days - since}
        if stage == "not_accepted":
            if since >= rejection_days:
                return {"action": "re_engage", "due": True, "urgency": "low",
                        "message": f"Re-engage — {since}d since rejection"}
            return {"action": "dormant", "due": False}
        if stage == "reply_received":
            return {"action": "replied", "due": False}
    elif channel == "email":
        email_followup = int(settings.get("followup_days", "4")) + 1
        if stage in ["email_sent", "followup_sent"]:
            if since >= email_followup:
                return {"action": "followup", "due": True, "urgency": "medium",
                        "days_overdue": since - email_followup, "message": f"Email follow-up due — {since}d since email"}
            return {"action": "waiting_reply", "due": False, "days_until": email_followup - since}

    return {"action": "unknown", "due": False}

# ─── EMAIL ──────────────────────────────────────────────────────────────────────
def send_email_bg(to: str, subject: str, html: str):
    if not SMTP_USER or not SMTP_PASS:
        print(f"[EMAIL SKIP] No SMTP config. Would send to {to}: {subject}")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"OutreachOS <{SMTP_USER}>"
        msg["To"] = to
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, to, msg.as_string())
        print(f"[EMAIL OK] Sent to {to}: {subject}")
    except Exception as e:
        print(f"[EMAIL ERR] {e}")

def verification_email(username: str, token: str) -> tuple[str, str]:
    url = f"{APP_URL}/verify?token={token}"
    subject = "Verify your OutreachOS account"
    html = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px;background:#07080a;color:#dde1e8;border-radius:12px">
      <h2 style="font-family:monospace;letter-spacing:3px;color:#b8ff57;margin-bottom:8px">OUTREACHOS</h2>
      <p style="color:#7a8292;font-size:14px;margin-bottom:24px">B2B Outreach Intelligence Platform</p>
      <h3 style="color:#dde1e8">Hey {username}, verify your email</h3>
      <p style="color:#7a8292;font-size:14px;line-height:1.7">You're one step away from tracking your outreach with OutreachOS.</p>
      <a href="{url}" style="display:inline-block;margin:24px 0;padding:12px 28px;background:#b8ff57;color:#07080a;text-decoration:none;border-radius:8px;font-weight:700;font-size:14px">Verify my email →</a>
      <p style="color:#404855;font-size:12px">If you didn't create an account, ignore this email.</p>
    </div>
    """
    return subject, html

# ─── MODELS ────────────────────────────────────────────────────────────────────
class SignupRequest(BaseModel):
    username: str
    email: EmailStr
    password: str

class LoginRequest(BaseModel):
    username: str
    password: str

class ContactCreate(BaseModel):
    first_name: str
    last_name: str
    company: Optional[str] = None
    role: Optional[str] = None
    linkedin_url: Optional[str] = None
    email: Optional[str] = None
    channel: str = "linkedin"
    category_id: Optional[str] = None
    subcategory: Optional[str] = None
    notes: Optional[str] = None
    connection_date: Optional[str] = None

class ContactUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    role: Optional[str] = None
    linkedin_url: Optional[str] = None
    email: Optional[str] = None
    category_id: Optional[str] = None
    subcategory: Optional[str] = None
    notes: Optional[str] = None
    connection_date: Optional[str] = None

class StageUpdate(BaseModel):
    stage: str
    note: Optional[str] = None

class CategoryCreate(BaseModel):
    name: str

class SubcategoryUpdate(BaseModel):
    subcategories: List[str]

class SettingsUpdate(BaseModel):
    groq_api_key: Optional[str] = None
    daily_request_target: Optional[int] = None
    dm_gap_days: Optional[int] = None
    followup_days: Optional[int] = None
    accept_check_days: Optional[int] = None
    rejection_recheck_days: Optional[int] = None

class ChatMessage(BaseModel):
    message: str
    session_id: Optional[str] = None

class ChatSessionCreate(BaseModel):
    name: str
    target_type: str = "general"

class LinkedInScrapeRequest(BaseModel):
    linkedin_url: str

# ─── AUTH ROUTES ───────────────────────────────────────────────────────────────
@app.post("/auth/signup")
async def signup(body: SignupRequest, background_tasks: BackgroundTasks):
    if len(body.username) < 3:
        raise HTTPException(400, "Username must be at least 3 characters")
    if len(body.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")

    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id FROM users WHERE username=$1 OR email=$2",
            body.username.lower(), body.email.lower()
        )
        if existing:
            raise HTTPException(400, "Username or email already taken")

        user_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        pw_hash = hash_password(body.password)

        await conn.execute(
            "INSERT INTO users (id, username, email, password_hash, verification_token) VALUES ($1,$2,$3,$4,$5)",
            user_id, body.username.lower(), body.email.lower(), pw_hash, token
        )

        default_settings = [
            (user_id, "groq_api_key", ""),
            (user_id, "daily_request_target", "30"),
            (user_id, "dm_gap_days", "2"),
            (user_id, "followup_days", "4"),
            (user_id, "accept_check_days", "3"),
            (user_id, "rejection_recheck_days", "45"),
        ]
        await conn.executemany(
            "INSERT INTO settings (user_id, key, value) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
            default_settings
        )

        default_cats = [
            (f"cat_{user_id[:8]}_ecom", user_id, "Ecommerce", json.dumps(["Amazon","Walmart","Flipkart","Shopify"])),
            (f"cat_{user_id[:8]}_ai",   user_id, "AI / ML",   json.dumps(["LLM Platforms","AI Agents","Data Infra"])),
            (f"cat_{user_id[:8]}_saas", user_id, "SaaS",      json.dumps(["CRM","Marketing","HR Tech"])),
            (f"cat_{user_id[:8]}_inv",  user_id, "Investment", json.dumps(["VC","Angel","Corporate VC"])),
            (f"cat_{user_id[:8]}_job",  user_id, "Job Hunt",   json.dumps(["Full-Time","Internship","Contract"])),
        ]
        await conn.executemany(
            "INSERT INTO categories (id, user_id, name, subcategories) VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING",
            default_cats
        )

        # Create default chat session
        default_session_id = f"sess_{user_id[:8]}_default"
        await conn.execute(
            "INSERT INTO chat_sessions (id, user_id, name, target_type) VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING",
            default_session_id, user_id, "General Outreach", "general"
        )

    subject, html = verification_email(body.username, token)
    background_tasks.add_task(send_email_bg, body.email, subject, html)

    return {"message": "Account created! Check your email to verify your account."}

@app.post("/auth/login")
async def login(body: LoginRequest):
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT * FROM users WHERE username=$1", body.username.lower()
        )
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "Invalid username or password")
    if not user["is_verified"]:
        raise HTTPException(403, "Please verify your email before logging in")

    token = create_token(user["id"], user["username"])
    return {"token": token, "username": user["username"]}

@app.get("/auth/verify")
async def verify_email(token: str):
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE verification_token=$1", token)
        if not user:
            raise HTTPException(400, "Invalid or expired verification link")
        await conn.execute(
            "UPDATE users SET is_verified=TRUE, verification_token=NULL WHERE id=$1",
            user["id"]
        )
    from fastapi.responses import HTMLResponse
    return HTMLResponse("""
    <html><head><meta http-equiv="refresh" content="2;url=/" /></head>
    <body style="font-family:sans-serif;background:#07080a;color:#b8ff57;display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;gap:12px">
      <h2 style="font-family:monospace;letter-spacing:3px">✓ EMAIL VERIFIED</h2>
      <p style="color:#7a8292">Redirecting to OutreachOS...</p>
    </body></html>
    """)

@app.post("/auth/resend-verification")
async def resend_verification(body: LoginRequest, background_tasks: BackgroundTasks):
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE username=$1", body.username.lower())
        if not user or not verify_password(body.password, user["password_hash"]):
            raise HTTPException(401, "Invalid credentials")
        if user["is_verified"]:
            return {"message": "Account already verified"}
        token = secrets.token_urlsafe(32)
        await conn.execute("UPDATE users SET verification_token=$1 WHERE id=$2", token, user["id"])

    subject, html = verification_email(user["username"], token)
    background_tasks.add_task(send_email_bg, user["email"], subject, html)
    return {"message": "Verification email sent"}

@app.get("/auth/me")
async def me(user=Depends(get_current_user)):
    return {"id": user["id"], "username": user["username"], "email": user["email"]}

# ─── CONTACTS ──────────────────────────────────────────────────────────────────
async def fetch_user_settings(conn, user_id: str) -> dict:
    rows = await conn.fetch("SELECT key, value FROM settings WHERE user_id=$1", user_id)
    return {r["key"]: r["value"] for r in rows}

def enrich_contact(c: dict, settings: dict) -> dict:
    c["playbook"] = compute_playbook_status(c, settings)
    c["days_since_action"] = days_since(c.get("last_action_date"))
    for k in ["added_date", "last_action_date", "connection_date"]:
        if c.get(k) and not isinstance(c[k], str):
            c[k] = c[k].isoformat()
    return c

@app.get("/contacts")
async def list_contacts(user=Depends(get_current_user)):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM contacts WHERE user_id=$1 ORDER BY added_date DESC", user["id"])
        settings = await fetch_user_settings(conn, user["id"])
    result = [enrich_contact(dict(r), settings) for r in rows]
    return result

@app.post("/contacts")
async def create_contact(body: ContactCreate, user=Depends(get_current_user)):
    async with db_pool.acquire() as conn:
        cid = str(uuid.uuid4())
        now = datetime.utcnow()
        initial_stage = "email_sent" if body.channel == "email" else "request_sent"
        conn_date = None
        if body.connection_date:
            try:
                conn_date = datetime.fromisoformat(body.connection_date)
            except:
                pass
        await conn.execute(
            """INSERT INTO contacts
               (id,user_id,first_name,last_name,company,role,linkedin_url,email,channel,stage,
                category_id,subcategory,notes,connection_date,added_date,last_action_date)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)""",
            cid, user["id"], body.first_name, body.last_name, body.company, body.role,
            body.linkedin_url, body.email, body.channel, initial_stage,
            body.category_id, body.subcategory, body.notes, conn_date, now, now
        )
        await conn.execute(
            "INSERT INTO stage_history (contact_id, stage, note, date) VALUES ($1,$2,$3,$4)",
            cid, initial_stage, "Contact added", now
        )
        if body.channel == "linkedin":
            await conn.execute(
                """INSERT INTO daily_log (user_id, log_date, requests_sent)
                   VALUES ($1, CURRENT_DATE, 1)
                   ON CONFLICT (user_id, log_date) DO UPDATE SET requests_sent = daily_log.requests_sent + 1""",
                user["id"]
            )
    return {"id": cid, "message": "Contact created"}

@app.put("/contacts/{cid}")
async def update_contact(cid: str, body: ContactUpdate, user=Depends(get_current_user)):
    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT id FROM contacts WHERE id=$1 AND user_id=$2", cid, user["id"])
        if not existing:
            raise HTTPException(404, "Contact not found")
        fields = {k: v for k, v in body.dict().items() if v is not None}
        if "connection_date" in fields:
            try:
                fields["connection_date"] = datetime.fromisoformat(fields["connection_date"])
            except:
                del fields["connection_date"]
        if fields:
            sets = ", ".join(f"{k}=${i+2}" for i, k in enumerate(fields))
            vals = list(fields.values()) + [cid]
            await conn.execute(f"UPDATE contacts SET {sets} WHERE id=${len(vals)}", *vals)
    return {"message": "Updated"}

@app.delete("/contacts/{cid}")
async def delete_contact(cid: str, user=Depends(get_current_user)):
    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT id FROM contacts WHERE id=$1 AND user_id=$2", cid, user["id"])
        if not existing:
            raise HTTPException(404, "Contact not found")
        await conn.execute("DELETE FROM contacts WHERE id=$1", cid)
    return {"message": "Deleted"}

@app.post("/contacts/{cid}/stage")
async def update_stage(cid: str, body: StageUpdate, user=Depends(get_current_user)):
    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT id FROM contacts WHERE id=$1 AND user_id=$2", cid, user["id"])
        if not existing:
            raise HTTPException(404, "Contact not found")
        now = datetime.utcnow()
        is_rejected = body.stage == "not_accepted"
        conn_date_update = ""
        if body.stage == "request_accepted":
            conn_date_update = ", connection_date=$5"
            await conn.execute(
                f"UPDATE contacts SET stage=$1, last_action_date=$2, is_rejected=$3, connection_date=$4 WHERE id=$5",
                body.stage, now, is_rejected, now, cid
            )
        else:
            await conn.execute(
                "UPDATE contacts SET stage=$1, last_action_date=$2, is_rejected=$3 WHERE id=$4",
                body.stage, now, is_rejected, cid
            )
        await conn.execute(
            "INSERT INTO stage_history (contact_id, stage, note, date) VALUES ($1,$2,$3,$4)",
            cid, body.stage, body.note or "", now
        )
        if body.stage in ["followup_sent", "dm_sent"]:
            await conn.execute(
                """INSERT INTO daily_log (user_id, log_date, requests_sent)
                   VALUES ($1, CURRENT_DATE, 0)
                   ON CONFLICT (user_id, log_date) DO NOTHING""",
                user["id"]
            )
    return {"message": "Stage updated"}

@app.get("/contacts/{cid}/history")
async def get_history(cid: str, user=Depends(get_current_user)):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM stage_history WHERE contact_id=$1 ORDER BY date DESC", cid
        )
    return [dict(r) for r in rows]

# ─── LINKEDIN SCRAPE (AI-powered extraction) ───────────────────────────────────
@app.post("/linkedin/extract")
async def extract_linkedin(body: LinkedInScrapeRequest, user=Depends(get_current_user)):
    """Use Groq to extract structured contact info from a LinkedIn URL pattern + ask user to paste profile text."""
    client = await get_groq_client(user["id"])
    url = body.linkedin_url.strip()

    # Parse the username from the URL
    match = re.search(r'linkedin\.com/in/([^/?#]+)', url)
    li_username = match.group(1).replace("-", " ").title() if match else ""

    system = """You are a LinkedIn profile parser. Given a LinkedIn profile URL or username, extract likely contact information.
Return ONLY a JSON object with these fields: first_name, last_name, company, role, linkedin_url.
Make educated guesses from the username pattern. Return null for fields you cannot determine."""

    prompt = f"""LinkedIn URL: {url}
Username hint: {li_username}

Return ONLY valid JSON like:
{{"first_name": "John", "last_name": "Doe", "company": null, "role": null, "linkedin_url": "{url}"}}"""

    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            max_tokens=256,
            temperature=0.1,
        )
        raw = response.choices[0].message.content.strip()
        # Extract JSON from response
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            data["linkedin_url"] = url
            return {"success": True, "data": data}
        return {"success": False, "error": "Could not parse profile"}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ─── DASHBOARD ─────────────────────────────────────────────────────────────────
@app.get("/dashboard/today")
async def today_dashboard(user=Depends(get_current_user)):
    async with db_pool.acquire() as conn:
        all_rows = await conn.fetch("SELECT * FROM contacts WHERE user_id=$1", user["id"])
        settings = await fetch_user_settings(conn, user["id"])
        today_log = await conn.fetchrow(
            "SELECT requests_sent FROM daily_log WHERE user_id=$1 AND log_date=CURRENT_DATE",
            user["id"]
        )
        requests_sent_today = today_log["requests_sent"] if today_log else 0
        week_log = await conn.fetch(
            """SELECT log_date, requests_sent FROM daily_log
               WHERE user_id=$1 AND log_date >= CURRENT_DATE - INTERVAL '6 days'
               ORDER BY log_date""",
            user["id"]
        )

    all_contacts = [dict(r) for r in all_rows]
    target = int(settings.get("daily_request_target", "30"))

    check_acceptance, send_dm, followup_due, re_engage, waiting = [], [], [], [], []

    for c in all_contacts:
        for k in ["added_date", "last_action_date", "connection_date"]:
            if c.get(k) and not isinstance(c[k], str):
                c[k] = c[k].isoformat()
        ps = compute_playbook_status(c, settings)
        c["playbook"] = ps
        c["days_since"] = days_since(c.get("last_action_date"))
        if not ps.get("due"):
            if c["stage"] in ["dm_sent", "followup_sent", "email_sent"]:
                waiting.append(c)
            continue
        action = ps.get("action")
        if action == "check_acceptance": check_acceptance.append(c)
        elif action == "send_dm": send_dm.append(c)
        elif action == "followup": followup_due.append(c)
        elif action == "re_engage": re_engage.append(c)

    followup_due.sort(key=lambda c: c.get("days_since", 0), reverse=True)

    week_data = {}
    for row in week_log:
        week_data[str(row["log_date"])] = row["requests_sent"]

    return {
        "date": datetime.now().strftime("%A, %d %B %Y"),
        "daily_target": target,
        "requests_sent_today": requests_sent_today,
        "target_remaining": max(0, target - requests_sent_today),
        "total_active": len([c for c in all_contacts if c["stage"] not in ["reply_received", "not_accepted"]]),
        "total_replied": len([c for c in all_contacts if c["stage"] == "reply_received"]),
        "week_data": week_data,
        "buckets": {
            "followup_due": followup_due,
            "check_acceptance": check_acceptance,
            "send_dm": send_dm,
            "re_engage": re_engage,
            "waiting": waiting,
        }
    }

# ─── CATEGORIES ────────────────────────────────────────────────────────────────
@app.get("/categories")
async def list_categories(user=Depends(get_current_user)):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM categories WHERE user_id=$1 ORDER BY name", user["id"])
    result = []
    for r in rows:
        d = dict(r)
        d["subcategories"] = d.get("subcategories") or []
        result.append(d)
    return result

@app.post("/categories")
async def create_category(body: CategoryCreate, user=Depends(get_current_user)):
    async with db_pool.acquire() as conn:
        cid = f"cat_{str(uuid.uuid4())[:8]}"
        await conn.execute(
            "INSERT INTO categories (id, user_id, name, subcategories) VALUES ($1,$2,$3,$4)",
            cid, user["id"], body.name, json.dumps([])
        )
    return {"id": cid}

@app.put("/categories/{cid}/subcategories")
async def update_subcategories(cid: str, body: SubcategoryUpdate, user=Depends(get_current_user)):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE categories SET subcategories=$1 WHERE id=$2 AND user_id=$3",
            json.dumps(body.subcategories), cid, user["id"]
        )
    return {"message": "Updated"}

@app.delete("/categories/{cid}")
async def delete_category(cid: str, user=Depends(get_current_user)):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM categories WHERE id=$1 AND user_id=$2", cid, user["id"])
        await conn.execute("UPDATE contacts SET category_id=NULL WHERE category_id=$1 AND user_id=$2", cid, user["id"])
    return {"message": "Deleted"}

# ─── SETTINGS ──────────────────────────────────────────────────────────────────
@app.get("/settings")
async def get_settings(user=Depends(get_current_user)):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, value FROM settings WHERE user_id=$1", user["id"])
    s = {r["key"]: r["value"] for r in rows}
    key = s.get("groq_api_key", "")
    s["groq_key_set"] = bool(key)
    s["groq_key_preview"] = f"...{key[-4:]}" if key else ""
    s.pop("groq_api_key", None)
    return s

@app.post("/settings")
async def save_settings(body: SettingsUpdate, user=Depends(get_current_user)):
    async with db_pool.acquire() as conn:
        updates = body.dict(exclude_none=True)
        for k, v in updates.items():
            await conn.execute(
                "INSERT INTO settings (user_id, key, value) VALUES ($1,$2,$3) ON CONFLICT (user_id, key) DO UPDATE SET value=$3",
                user["id"], k, str(v)
            )
    return {"message": "Saved"}

# ─── GROQ AI ───────────────────────────────────────────────────────────────────
async def get_groq_client(user_id: str):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE user_id=$1 AND key='groq_api_key'", user_id)
    key = row["value"] if row else ""
    if not key:
        raise HTTPException(400, "Groq API key not set. Go to Settings.")
    return Groq(api_key=key)

def groq_complete(client: Groq, system: str, user_msg: str, history: list = None, use_web_search: bool = False) -> str:
    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_msg})

    tools = None
    if use_web_search:
        # Groq supports web search via tool calling pattern - simulate with a search-aware prompt
        # Since Groq doesn't have native web search, we enrich the system prompt
        pass

    response = client.chat.completions.create(
        model=GROQ_MODEL, messages=messages, max_tokens=1024, temperature=0.7,
    )
    return response.choices[0].message.content.strip()

# ─── CHAT SESSIONS ─────────────────────────────────────────────────────────────
@app.get("/chat/sessions")
async def list_sessions(user=Depends(get_current_user)):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM chat_sessions WHERE user_id=$1 ORDER BY updated_at DESC", user["id"]
        )
    return [dict(r) for r in rows]

@app.post("/chat/sessions")
async def create_session(body: ChatSessionCreate, user=Depends(get_current_user)):
    async with db_pool.acquire() as conn:
        sid = f"sess_{str(uuid.uuid4())[:12]}"
        await conn.execute(
            "INSERT INTO chat_sessions (id, user_id, name, target_type) VALUES ($1,$2,$3,$4)",
            sid, user["id"], body.name, body.target_type
        )
    return {"id": sid, "name": body.name, "target_type": body.target_type}

@app.delete("/chat/sessions/{sid}")
async def delete_session(sid: str, user=Depends(get_current_user)):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM chat_sessions WHERE id=$1 AND user_id=$2", sid, user["id"])
    return {"message": "Deleted"}

@app.put("/chat/sessions/{sid}")
async def rename_session(sid: str, body: ChatSessionCreate, user=Depends(get_current_user)):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE chat_sessions SET name=$1, target_type=$2, updated_at=NOW() WHERE id=$3 AND user_id=$4",
            body.name, body.target_type, sid, user["id"]
        )
    return {"message": "Updated"}

# ─── AI ROUTES ─────────────────────────────────────────────────────────────────
@app.post("/contacts/{cid}/draft")
async def generate_draft(cid: str, user=Depends(get_current_user)):
    client = await get_groq_client(user["id"])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM contacts WHERE id=$1 AND user_id=$2", cid, user["id"])
    if not row:
        raise HTTPException(404, "Contact not found")
    c = dict(row)
    stage = c["stage"]
    since = days_since(c.get("last_action_date"))

    stage_prompts = {
        "request_accepted": f"Write a short first LinkedIn DM to {c['first_name']} {c['last_name']} who just accepted my connection request. They work as {c.get('role','unknown role')} at {c.get('company','unknown company')}.",
        "dm_sent": f"Write a follow-up LinkedIn message to {c['first_name']} {c['last_name']} at {c.get('company','')} who hasn't responded to my first DM after {since} days.",
        "followup_sent": f"Write a creative follow-up message #{since//4+1} to {c['first_name']} {c['last_name']} at {c.get('company','')}. They haven't responded in {since} days. Try a new angle.",
        "email_sent": f"Write an email follow-up to {c['first_name']} {c['last_name']} at {c.get('company','')} who hasn't responded to my initial email after {since} days.",
    }

    instruction = stage_prompts.get(stage, f"Write an outreach message to {c['first_name']} {c['last_name']} at {c.get('company','')}.")
    system = "You are helping a young entrepreneur with outreach for their AI/ML consulting and software services business."
    user_prompt = f"""Contact details:
- Name: {c['first_name']} {c['last_name']}
- Role: {c.get('role','unknown')}
- Company: {c.get('company','unknown')}
- Channel: {c.get('channel','linkedin')}
- Notes/Context: {c.get('notes','none')}

Task: {instruction}

Rules:
- Keep it under 100 words
- Sound natural and human — not AI-generated
- Be specific about their role/company when possible
- End with a single clear, low-friction ask
- Write ONLY the message body, no subject line, no preamble"""

    try:
        draft = groq_complete(client, system, user_prompt)
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO ai_messages (contact_id, message_type, content) VALUES ($1,$2,$3)",
                cid, stage, draft
            )
        return {"draft": draft, "stage": stage}
    except Exception as e:
        raise HTTPException(500, f"Groq error: {str(e)}")

@app.get("/contacts/{cid}/drafts")
async def get_drafts(cid: str, user=Depends(get_current_user)):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM ai_messages WHERE contact_id=$1 ORDER BY created_at DESC LIMIT 5", cid
        )
    return [dict(r) for r in rows]

@app.post("/ai/rulebook")
async def generate_rulebook(user=Depends(get_current_user)):
    client = await get_groq_client(user["id"])
    system = "You are an expert outreach strategist for a B2B sales and consulting business."
    prompt = """Generate a structured outreach rulebook for LinkedIn and Email outreach. The user's context:
- They offer AI/ML consulting, software development, and SaaS services
- They target companies in Ecommerce (Amazon, Walmart), AI/ML, SaaS, Investment sectors
- Their daily goal is to send 30 LinkedIn connection requests
- Follow-up is INFINITE until explicitly rejected
- After rejection, re-engage after 45 days

Create a rulebook with:
1. LinkedIn outreach rules (request to acceptance to DM to follow-up cycle)
2. Email outreach rules
3. Follow-up timing guidelines
4. Message tone guidelines for each stage
5. When to mark someone as truly closed vs keep following up

Format as clear, numbered rules. Be specific with timing. Keep it practical."""

    try:
        rulebook = groq_complete(client, system, prompt)
        return {"rulebook": rulebook}
    except Exception as e:
        raise HTTPException(500, f"Groq error: {str(e)}")

@app.post("/ai/chat")
async def ai_chat(body: ChatMessage, user=Depends(get_current_user)):
    client = await get_groq_client(user["id"])

    # Determine session
    session_id = body.session_id
    async with db_pool.acquire() as conn:
        if session_id:
            sess = await conn.fetchrow("SELECT * FROM chat_sessions WHERE id=$1 AND user_id=$2", session_id, user["id"])
            if not sess:
                session_id = None

        if not session_id:
            # Get or create default session
            default_sess = await conn.fetchrow(
                "SELECT id FROM chat_sessions WHERE user_id=$1 ORDER BY created_at ASC LIMIT 1", user["id"]
            )
            if default_sess:
                session_id = default_sess["id"]
            else:
                session_id = f"sess_{str(uuid.uuid4())[:12]}"
                await conn.execute(
                    "INSERT INTO chat_sessions (id, user_id, name, target_type) VALUES ($1,$2,$3,$4)",
                    session_id, user["id"], "General Outreach", "general"
                )

        history_rows = await conn.fetch(
            "SELECT role, content FROM chat_history WHERE user_id=$1 AND session_id=$2 ORDER BY id DESC LIMIT 20",
            user["id"], session_id
        )
        history = [{"role": r["role"], "content": r["content"]} for r in reversed(history_rows)]

        all_contacts = await conn.fetch("SELECT stage FROM contacts WHERE user_id=$1", user["id"])
        settings = await fetch_user_settings(conn, user["id"])
        target = settings.get("daily_request_target", "30")

        # Get session info for context
        sess_info = await conn.fetchrow("SELECT name, target_type FROM chat_sessions WHERE id=$1", session_id)

    contacts_list = [dict(r) for r in all_contacts]
    replied = sum(1 for c in contacts_list if c["stage"] == "reply_received")
    active = sum(1 for c in contacts_list if c["stage"] not in ["reply_received", "not_accepted"])

    sess_name = sess_info["name"] if sess_info else "General"
    sess_type = sess_info["target_type"] if sess_info else "general"

    # Detect if the message needs live search context
    search_keywords = ["latest", "recent", "current", "news", "today", "2024", "2025", "trend", "update", "what is", "who is", "price", "stock"]
    needs_search = any(kw in body.message.lower() for kw in search_keywords)

    search_context = ""
    if needs_search:
        search_context = f"\n\nNOTE: The user may be asking about current/recent information. Today's date is {datetime.now().strftime('%B %d, %Y')}. Provide the most up-to-date information you know, and clearly indicate when your knowledge may be outdated."

    system = f"""You are an outreach strategy assistant integrated into OutreachOS — a LinkedIn/Email outreach tracking platform.

Current chat session: "{sess_name}" (target type: {sess_type})

Current dashboard stats:
- Total active contacts: {active}
- Replied: {replied}
- Total contacts: {len(contacts_list)}
- Daily request target: {target}

The user's SOP:
1. Send LinkedIn connection requests (target: {target}/day)
2. Wait for acceptance (check after 3 days)
3. Send DM within 2 days of acceptance
4. Follow up every 4 days indefinitely until reply or explicit rejection
5. Re-engage rejected contacts after 45 days

This chat session is focused on: {sess_type} outreach targets.
Help them with: outreach strategy, message drafts, company suggestions, follow-up advice, industry research, and any questions they have.
Keep responses concise and actionable. Use markdown formatting for lists and structure.{search_context}"""

    try:
        reply = groq_complete(client, system, body.message, history=history)
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO chat_history (user_id, session_id, role, content) VALUES ($1,$2,$3,$4)",
                user["id"], session_id, "user", body.message
            )
            await conn.execute(
                "INSERT INTO chat_history (user_id, session_id, role, content) VALUES ($1,$2,$3,$4)",
                user["id"], session_id, "assistant", reply
            )
            await conn.execute(
                "UPDATE chat_sessions SET updated_at=NOW() WHERE id=$1", session_id
            )
        return {"reply": reply, "session_id": session_id}
    except Exception as e:
        raise HTTPException(500, f"Groq error: {str(e)}")

@app.get("/ai/chat/history")
async def get_chat_history(session_id: Optional[str] = None, user=Depends(get_current_user)):
    async with db_pool.acquire() as conn:
        if session_id:
            rows = await conn.fetch(
                "SELECT * FROM chat_history WHERE user_id=$1 AND session_id=$2 ORDER BY id DESC LIMIT 50",
                user["id"], session_id
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM chat_history WHERE user_id=$1 ORDER BY id DESC LIMIT 50", user["id"]
            )
    return list(reversed([dict(r) for r in rows]))

@app.delete("/ai/chat/history")
async def clear_chat_history(session_id: Optional[str] = None, user=Depends(get_current_user)):
    async with db_pool.acquire() as conn:
        if session_id:
            await conn.execute("DELETE FROM chat_history WHERE user_id=$1 AND session_id=$2", user["id"], session_id)
        else:
            await conn.execute("DELETE FROM chat_history WHERE user_id=$1", user["id"])
    return {"message": "Cleared"}

# ─── SERVE DASHBOARD ───────────────────────────────────────────────────────────
dash_path = os.path.join(os.path.dirname(__file__), "..", "dashboard")
if os.path.exists(dash_path):
    app.mount("/", StaticFiles(directory=dash_path, html=True), name="static")
