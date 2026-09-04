import logging
import json
import base64
import asyncio
import time
import os
import sqlite3
import urllib.request
import urllib.error
from contextlib import closing
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, Update
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, TypeHandler, ApplicationHandlerStop, filters,
)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("shadow-files")

CONFIG_PATH = Path(__file__).with_name("config.json")
with CONFIG_PATH.open("r", encoding="utf-8") as config_file:
    CONFIG = json.load(config_file)

BOT_TOKEN = str(CONFIG.get("bot_token", "")).strip()
OWNER_ID = int(CONFIG.get("owner_id", 0) or 0)


DB_PATH = ":memory:"
MEMORY_CONNECTION = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
MEMORY_CONNECTION.row_factory = sqlite3.Row
MEMORY_CONNECTION.execute("PRAGMA foreign_keys = ON")
WA_URL = str(CONFIG.get("whatsapp_channel", {}).get("url", "")).strip()
WA_NAME = str(CONFIG.get("whatsapp_channel", {}).get("name", "WhatsApp Channel"))
BANNER_URL = str(CONFIG.get("banner_url", "")).strip()
OWNER_CONTACT_URL = str(CONFIG.get("owner_contact_url", "")).strip()
GITHUB_CONFIG = CONFIG.get("github", {})
GITHUB_TOKEN = str(GITHUB_CONFIG.get("token", "")).strip() or os.getenv("GITHUB_TOKEN", "").strip()
REQUIRED = CONFIG.get("required", [])
CHANNELS = [x for x in REQUIRED if x.get("type") == "channel"]
GROUPS = [x for x in REQUIRED if x.get("type") == "group"]

ADD_CATEGORY, ADD_FILE, SET_FILE_NAME, SET_FILE_REFS, BROADCAST = range(5)

BLUE = "🔵"
GREEN = "🟢"
RED = "🔴"
GOLD = "🟡"


def button(text, style=None, callback_data=None, url=None):
    """Build a button with Telegram's primary/success/danger styles."""
    if style is None:
        if any(word in text.lower() for word in ("delete", "remove", "ban", "danger", "close")):
            style = "danger"
        elif any(word in text.lower() for word in ("add", "purchase", "refer", "verify", "join", "unban")):
            style = "success"
        else:
            style = "primary"
    kwargs = {"style": style}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    return InlineKeyboardButton(text, **kwargs)


def chat_spec(value):
    """Allow CHAT_ID or CHAT_ID|INVITE_URL in environment variables."""
    if isinstance(value, dict):
        return value.get("chat_id", "").strip(), value.get("join_url"), value.get("name", value.get("chat_id", ""))
    parts = value.split("|", 1)
    chat_id = parts[0].strip()
    if chat_id.startswith("https://t.me/") and "/+" not in chat_id:
        chat_id = "@" + chat_id.rstrip("/").split("/")[-1]
    if chat_id and not chat_id.startswith("@") and not chat_id.startswith("-") and not chat_id.startswith("http"):
        chat_id = f"@{chat_id}"
    return chat_id, parts[1].strip() if len(parts) == 2 else None, chat_id


def db():
    return MEMORY_CONNECTION


STATE_TABLES = ("users", "roles", "categories", "files", "purchases", "referrals")
LAST_SYNC_ERROR = None
SYNC_PAUSED_UNTIL = 0.0
SYNC_FAILURE_NOTIFIED = False
SYNC_COOLDOWN_SECONDS = 3600


def state_payload():
    payload = {}
    with db() as c:
        for table in STATE_TABLES:
            rows = c.execute(f"SELECT * FROM {table}").fetchall()
            payload[table] = [dict(row) for row in rows]
    return payload


def github_request(method, url, token, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode())


def github_state_sync():
    token = GITHUB_TOKEN
    repo = str(GITHUB_CONFIG.get("repo", "")).strip()
    state_file = str(GITHUB_CONFIG.get("state_file", "bot_state.json")).strip()
    if not token or not repo: return
    api = f"https://api.github.com/repos/{repo}/contents/{state_file}"
    encoded = base64.b64encode(json.dumps(state_payload(), ensure_ascii=False).encode()).decode()
    current = None
    try: current = github_request("GET", api, token)
    except urllib.error.HTTPError as exc:
        if exc.code != 404: raise
    body = {"message": "Update bot state", "content": encoded}
    if current and current.get("sha"): body["sha"] = current["sha"]
    github_request("PUT", api, token, body)


def github_state_restore():
    token = GITHUB_TOKEN
    repo = str(GITHUB_CONFIG.get("repo", "")).strip()
    state_file = str(GITHUB_CONFIG.get("state_file", "bot_state.json")).strip()
    if not token or not repo: return
    api = f"https://api.github.com/repos/{repo}/contents/{state_file}"
    try:
        raw = github_request("GET", api, token)
        data = json.loads(base64.b64decode(raw["content"]).decode())
        with db() as c:
            for table in STATE_TABLES:
                rows = data.get(table, [])
                if not rows: continue
                columns = list(rows[0].keys())
                placeholders = ",".join("?" for _ in columns)
                c.executemany(f"INSERT OR REPLACE INTO {table} ({','.join(columns)}) VALUES ({placeholders})", [[row.get(col) for col in columns] for row in rows])
            c.commit()
    except urllib.error.HTTPError as exc:
        if exc.code != 404: log.warning("GitHub state restore failed: %s", exc)
    except Exception as exc:
        log.warning("GitHub state restore failed: %s", exc)


async def periodic_state_sync(context):
    global LAST_SYNC_ERROR, SYNC_PAUSED_UNTIL, SYNC_FAILURE_NOTIFIED
    if time.time() < SYNC_PAUSED_UNTIL:
        if LAST_SYNC_ERROR and not SYNC_FAILURE_NOTIFIED:
            try:
                await context.bot.send_message(OWNER_ID, f"GitHub data upload fail ho gaya.\n\nReason: {LAST_SYNC_ERROR}\n\nSafety cooldown 1 hour ke liye active hai. Is duration mein uploads pause rahenge; cooldown ke baad bot automatically retry karega.")
                SYNC_FAILURE_NOTIFIED = True
            except Exception: pass
        return
    try:
        await asyncio.to_thread(github_state_sync)
        if LAST_SYNC_ERROR:
            try: await context.bot.send_message(OWNER_ID, "GitHub data upload successfully restore ho gaya. Cooldown ke baad pending data upload ho chuka hai.")
            except Exception: pass
        LAST_SYNC_ERROR = None
        SYNC_PAUSED_UNTIL = 0.0
        SYNC_FAILURE_NOTIFIED = False
    except Exception as exc:
        reason = str(exc)
        log.warning("GitHub state sync failed: %s", reason)
        LAST_SYNC_ERROR = reason
        SYNC_PAUSED_UNTIL = time.time() + SYNC_COOLDOWN_SECONDS
        SYNC_FAILURE_NOTIFIED = False


async def save_state_now():
    global LAST_SYNC_ERROR, SYNC_PAUSED_UNTIL, SYNC_FAILURE_NOTIFIED
    if GITHUB_TOKEN and GITHUB_CONFIG.get("repo"):
        if time.time() < SYNC_PAUSED_UNTIL: return
        try: await asyncio.to_thread(github_state_sync)
        except Exception as exc:
            LAST_SYNC_ERROR = str(exc)
            SYNC_PAUSED_UNTIL = time.time() + SYNC_COOLDOWN_SECONDS
            SYNC_FAILURE_NOTIFIED = False
            log.warning("Immediate GitHub state sync failed; cooldown enabled: %s", exc)


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, referrals INTEGER NOT NULL DEFAULT 0,
            banned INTEGER NOT NULL DEFAULT 0, joined_gate INTEGER NOT NULL DEFAULT 0, premium INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, last_seen TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS roles (user_id INTEGER PRIMARY KEY, role TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
            created_by INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY AUTOINCREMENT, category_id INTEGER NOT NULL,
            name TEXT NOT NULL, file_id TEXT NOT NULL, file_type TEXT NOT NULL, required_refs INTEGER NOT NULL DEFAULT 0,
            created_by INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS purchases (user_id INTEGER, file_id INTEGER, purchased_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id, file_id), FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS referrals (inviter_id INTEGER, invitee_id INTEGER UNIQUE, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(inviter_id) REFERENCES users(id), FOREIGN KEY(invitee_id) REFERENCES users(id));
        """)
        try: c.execute("ALTER TABLE users ADD COLUMN premium INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError: pass
        c.commit()


def upsert_user(u):
    with db() as c:
        c.execute("INSERT INTO users(id, username, first_name) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, last_seen=CURRENT_TIMESTAMP", (u.id, u.username or "", u.first_name or ""))
        c.commit()


def get_role(uid: int) -> str:
    if uid == OWNER_ID:
        return "owner"
    with db() as c:
        row = c.execute("SELECT role FROM roles WHERE user_id=?", (uid,)).fetchone()
        return row["role"] if row else "user"


def is_staff(uid): return get_role(uid) in {"owner", "admin", "partner"}
def allowed(uid, action):
    role = get_role(uid)
    matrix = {"category": {"owner", "admin", "partner"}, "file": {"owner", "admin", "partner"}, "broadcast": {"owner", "admin", "partner"}, "admin_manage": {"owner", "partner"}, "premium_manage": {"owner", "admin", "partner"}, "user_manage": {"owner"}}
    return role in matrix.get(action, set())


def categories():
    with db() as c: return c.execute("SELECT * FROM categories ORDER BY id DESC").fetchall()

def files_for(cid):
    with db() as c: return c.execute("SELECT * FROM files WHERE category_id=? ORDER BY id DESC", (cid,)).fetchall()

def user_refs(uid):
    with db() as c: return c.execute("SELECT referrals FROM users WHERE id=?", (uid,)).fetchone()["referrals"]


def user_status(uid):
    if uid == OWNER_ID: return "Owner"
    with db() as c:
        row = c.execute("SELECT premium FROM users WHERE id=?", (uid,)).fetchone()
    if row and row["premium"]: return "Premium User"
    role = get_role(uid)
    return {"admin": "Admin", "partner": "Partner"}.get(role, "Free User")


def is_premium(uid):
    with db() as c:
        row = c.execute("SELECT premium FROM users WHERE id=?", (uid,)).fetchone()
    return bool(row and row["premium"])


def is_banned(uid):
    with db() as c:
        row = c.execute("SELECT banned FROM users WHERE id=?", (uid,)).fetchone()
    return bool(row and row["banned"])


def category_keyboard():
    items = categories()
    styles = ("success", "primary", "danger")
    rows = []
    for index in range(0, len(items), 2):
        row = []
        for offset, item in enumerate(items[index:index + 2]):
            row.append(button(item["name"], styles[(index + offset) % len(styles)], callback_data=f"cat:{item['id']}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def dashboard_keyboard():
    rows = [[KeyboardButton("🔗 Refer Link"), KeyboardButton("💰 My Balance")], [KeyboardButton("👤 My Account")]]
    rows.append([KeyboardButton("💎 Contact Owner to Buy Premium")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


async def send_dashboard(target, context, uid, edit=False):
    text = f"<b>Welcome to Shadow Files Store</b>\n\n<b>Status:</b> {user_status(uid)}\n<b>Referral Balance:</b> {user_refs(uid)}"
    if edit:
        await target.edit_message_text(text, parse_mode="HTML")
    elif BANNER_URL:
        await context.bot.send_photo(uid, BANNER_URL, caption=text, parse_mode="HTML", reply_markup=dashboard_keyboard())
    else:
        await context.bot.send_message(uid, text, parse_mode="HTML", reply_markup=dashboard_keyboard())
    await context.bot.send_message(uid, "<b>Categories</b>", parse_mode="HTML", reply_markup=category_keyboard())


def gate_keyboard():
    rows = []
    styles = ("success", "primary", "danger")
    for i, chat in enumerate(CHANNELS + GROUPS):
        chat_id, invite, display_name = chat_spec(chat)
        label = f"Join {display_name}"
        link = invite or (chat_id if chat_id.startswith("http") else f"https://t.me/{chat_id.lstrip('@')}")
        rows.append([button(label, styles[i % len(styles)], url=link)])
    if WA_URL:
        rows.append([button("Join WhatsApp Channel (Required)", styles[(len(CHANNELS) + len(GROUPS)) % len(styles)], url=WA_URL)])
    verify_style = styles[(len(CHANNELS) + len(GROUPS) + (1 if WA_URL else 0)) % len(styles)]
    rows.append([button("Verify Membership", verify_style, callback_data="verify_gate")])
    return InlineKeyboardMarkup(rows)

async def check_gate(bot, uid: int):
    required_chats = list(CHANNELS) + list(GROUPS)
    if not required_chats: return True, None
    for chat in required_chats:
        chat_id, _, display_name = chat_spec(chat)
        try:
            # Resolve public usernames first. This avoids false negatives caused by
            # username aliases/renames and makes the membership lookup deterministic.
            resolved = await bot.get_chat(chat_id)
            canonical_id = resolved.id
            m = await bot.get_chat_member(canonical_id, uid)
            log.info("Membership check chat=%s canonical=%s user=%s status=%s is_member=%s", display_name, canonical_id, uid, m.status, getattr(m, "is_member", None))
            status = getattr(m.status, "value", str(m.status)).lower()
            if status in {"creator", "administrator", "member"}:
                continue
            if status == "restricted" and getattr(m, "is_member", False):
                continue
            # Every other status—including left, kicked, unknown, or a
            # restricted member who is no longer joined—must fail the gate.
            if status not in {"creator", "administrator", "member", "restricted"} or status in {"left", "kicked", "restricted"}:
                return False, display_name
        except Exception as e:
            log.warning("Gate check failed for chat=%s user=%s: %s", chat_id, uid, repr(e))
            return False, display_name
    return True, None


async def global_membership_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-check all required communities before every user interaction."""
    user = update.effective_user
    if not user:
        return
    query = update.callback_query
    # Users must be able to press Verify after joining again.
    if query and query.data == "verify_gate":
        return
    passed, failed_name = await check_gate(context.bot, user.id)
    if passed:
        return
    message = f"Aapka access block hai. Aap ne **{failed_name}** join nahi kiya. Isay join karein, phir Verify karein."
    if query:
        await query.answer("Membership incomplete", show_alert=True)
        await query.edit_message_text(message, reply_markup=gate_keyboard())
    elif update.effective_message:
        await update.effective_message.reply_text(message, reply_markup=gate_keyboard())
    raise ApplicationHandlerStop


async def enforce_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    query = update.callback_query
    if query and query.data == "verify_gate":
        return True
    user = update.effective_user
    if not user:
        return True
    passed, failed_name = await check_gate(context.bot, user.id)
    if passed:
        return True
    message = f"Aapka access block hai. Aap ne **{failed_name}** join nahi kiya. Isay join karein, phir Verify karein."
    if query:
        await query.answer("Membership incomplete", show_alert=True)
        await query.edit_message_text(message, reply_markup=gate_keyboard())
    elif update.effective_message:
        await update.effective_message.reply_text(message, reply_markup=gate_keyboard())
    return False

async def gate_or_prompt(update, context) -> bool:
    uid = update.effective_user.id
    passed, failed_name = await check_gate(context.bot, uid)
    if passed:
        with db() as c:
            c.execute("UPDATE users SET joined_gate=1 WHERE id=?", (uid,)); c.commit()
        return True
    text = f"Neeche tamam WhatsApp aur Telegram channels/groups join karein, phir Verify karein.\n\n{RED} Aap ne abhi **{failed_name}** join nahi kiya. Isay join karein, phir Verify karein."
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=gate_keyboard())
    else:
        await update.effective_message.reply_text(text, reply_markup=gate_keyboard())
    return False


def user_menu():
    rows = [[button(f"Categories", callback_data="categories"), button(f"Refer & Earn", callback_data="refer")]]
    return InlineKeyboardMarkup(rows)

def staff_menu(uid):
    role = get_role(uid)
    rows = [[button(f"Add Category", callback_data="add_category"), button(f"Remove Category", callback_data="remove_category")], [button(f"Add File", callback_data="add_file"), button(f"Delete File", callback_data="delete_file")], [button(f"Add Premium", callback_data="add_premium"), button(f"Remove Premium", callback_data="remove_premium")], [button(f"Broadcast", callback_data="broadcast")]]
    if role in {"owner", "partner"}: rows.append([button(f"Add Admin", callback_data="add_admin"), button(f"Remove Admin", callback_data="remove_admin")])
    if role == "owner": rows.extend([[button(f"Manage Partners", callback_data="partners")], [button(f"Ban/Unban User", callback_data="ban")], [button(f"Live Stats", callback_data="stats")]])
    rows.append([button("⬅️ User Panel", callback_data="home")])
    return InlineKeyboardMarkup(rows)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; upsert_user(u)
    if is_banned(u.id):
        await update.effective_message.reply_text("Aapka bot access band hai.")
        return
    # /start REFERRER_ID records only after successful gate verification.
    if context.args and context.args[0].isdigit() and int(context.args[0]) != u.id:
        context.user_data["pending_referrer"] = int(context.args[0])
    if not await gate_or_prompt(update, context): return
    ref = context.user_data.pop("pending_referrer", None)
    if ref:
        with db() as c:
            exists = c.execute("SELECT 1 FROM referrals WHERE invitee_id=?", (u.id,)).fetchone()
            if not exists and c.execute("SELECT 1 FROM users WHERE id=?", (ref,)).fetchone():
                c.execute("INSERT INTO referrals(inviter_id, invitee_id) VALUES(?,?)", (ref, u.id)); c.execute("UPDATE users SET referrals=referrals+1 WHERE id=?", (ref,)); c.commit()
                await save_state_now()
                try: await context.bot.send_message(ref, f"{GREEN} Aapka referral verify ho gaya! +1 point.")
                except Exception: pass
    await send_dashboard(update.effective_message, context, u.id)

async def verify(update, context):
    q = update.callback_query; await q.answer()
    passed, failed_name = await check_gate(context.bot, q.from_user.id)
    if passed:
        with db() as c: c.execute("UPDATE users SET joined_gate=1 WHERE id=?", (q.from_user.id,)); c.commit()
        await q.edit_message_text("<b>Verification successful.</b>", parse_mode="HTML")
        await send_dashboard(q.message, context, q.from_user.id)
    else: await q.edit_message_text(f"{RED} Aap ne abhi **{failed_name}** join nahi kiya. Isay join karein, phir Verify karein.", reply_markup=gate_keyboard())

async def commands(update, context):
    if not await enforce_membership(update, context): return
    uid = update.effective_user.id
    if update.message.text.startswith("/owner") or update.message.text.startswith("/admin") or update.message.text.startswith("/partner"):
        requested = update.message.text[1:].split()[0]
        if requested == "owner" and uid == OWNER_ID or requested == get_role(uid):
            await update.message.reply_text(f"{GOLD} {requested.title()} Panel", reply_markup=staff_menu(uid))
        else: await update.message.reply_text(f"{RED} Aap {requested} nahi hain.")

async def home(update, context):
    q=update.callback_query; await q.answer(); await q.edit_message_text(f"{GREEN} User Panel", reply_markup=user_menu())

async def show_categories(update, context):
    q=update.callback_query; await q.answer()
    rows=[[button(f"📁 {x['name']}", callback_data=f"cat:{x['id']}")] for x in categories()]
    rows.append([button("⬅️ Back", callback_data="home")])
    await q.edit_message_text("📁 Categories select karein:", reply_markup=InlineKeyboardMarkup(rows))

async def show_files(update, context):
    q=update.callback_query; await q.answer(); cid=int(q.data.split(":")[1]); rows=[[button(f"📄 {x['name']} — {x['required_refs']} refs", callback_data=f"file:{x['id']}")] for x in files_for(cid)]
    rows.append([button("⬅️ Categories", callback_data="categories")]); await q.edit_message_text("📄 Files:", reply_markup=InlineKeyboardMarkup(rows))

async def show_file(update, context):
    q=update.callback_query; await q.answer(); fid=int(q.data.split(":")[1]); uid=q.from_user.id
    with db() as c:
        f=c.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone(); bought=c.execute("SELECT 1 FROM purchases WHERE user_id=? AND file_id=?", (uid,fid)).fetchone()
    if not f: return
    if bought or is_premium(uid) or user_refs(uid) >= f["required_refs"]:
        await q.edit_message_text(f"{GREEN} {f['name']} ready hai. Purchase karke file receive karein.", reply_markup=InlineKeyboardMarkup([[button(f"Purchase / Get File", callback_data=f"buy:{fid}")],[button("⬅️ Files", callback_data=f"cat:{f['category_id']}")]]))
    else:
        await q.edit_message_text(f"{RED} Ye file locked hai.\nRequired referrals: {f['required_refs']}\nAapke referrals: {user_refs(uid)}", reply_markup=InlineKeyboardMarkup([[button(f"Refer Link", callback_data="refer")],[button("⬅️ Files", callback_data=f"cat:{f['category_id']}")]]))

async def refer(update, context):
    q=update.callback_query; await q.answer(); me=await context.bot.get_me(); link=f"https://t.me/{me.username}?start={q.from_user.id}"; await q.edit_message_text(f"{BLUE} Aapka unique referral link:\n\n{link}\n\nDost ko link bhejein. Referral tab count hoga jab wo 4 Telegram communities join karke verify kare.\n\nCurrent points: {user_refs(q.from_user.id)}", reply_markup=InlineKeyboardMarkup([[button("⬅️ Home", callback_data="home")]]))


async def account(update, context):
    q = update.callback_query; await q.answer()
    await q.edit_message_text(f"<b>My Account</b>\n\n<b>User ID:</b> <code>{q.from_user.id}</code>\n<b>Status:</b> {user_status(q.from_user.id)}\n<b>Referral Balance:</b> {user_refs(q.from_user.id)}", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[button("Back", callback_data="home")]]))

async def panel_action(update, context):
    q=update.callback_query; await q.answer(); uid=q.from_user.id; action=q.data
    if action in {"add_category","remove_category"} and allowed(uid,"category"):
        if action=="add_category": context.user_data["state"]=ADD_CATEGORY; await q.edit_message_text("Category ka naam bhejein:")
        else:
            rows=[[button(f"{x['name']}", callback_data=f"rmcat:{x['id']}")] for x in categories()]; await q.edit_message_text("Remove karne wali category select karein:", reply_markup=InlineKeyboardMarkup(rows))
    elif action in {"add_file","delete_file"} and allowed(uid,"file"):
        if action=="add_file":
            rows=[[button(x['name'], callback_data=f"newfilecat:{x['id']}")] for x in categories()]
            rows.append([button("Add a new category", "success", callback_data="addfile_newcat")])
            context.user_data["state"]=ADD_FILE; await q.edit_message_text("File kis category mein add karni hai?", reply_markup=InlineKeyboardMarkup(rows))
        else:
            rows=[[button(f"📄 {x['name']}", callback_data=f"rmfile:{x['id']}")] for cat in categories() for x in files_for(cat['id'])]; await q.edit_message_text("Delete karne wali file:", reply_markup=InlineKeyboardMarkup(rows))
    elif action=="broadcast" and allowed(uid,"broadcast"): context.user_data["state"]=BROADCAST; await q.edit_message_text("Broadcast message bhejein:")
    elif action == "partners" and uid == OWNER_ID:
        await q.edit_message_text("Partner Management:", reply_markup=InlineKeyboardMarkup([[button("Add Partner", "success", callback_data="add_partner"), button("Remove Partner", "danger", callback_data="remove_partner")], [button("Back", callback_data="owner_back")]]))
    elif action in {"add_partner","remove_partner"} and uid == OWNER_ID:
        context.user_data.pop("state", None); context.user_data["role_action"] = action; await q.edit_message_text("Partner user ka numeric Telegram ID bhejein:")
    elif action == "ban" and uid == OWNER_ID:
        await q.edit_message_text("User Management:", reply_markup=InlineKeyboardMarkup([[button("Ban User", "danger", callback_data="ban_user"), button("Unban User", "success", callback_data="unban_user")], [button("Back", callback_data="owner_back")]]))
    elif action in {"ban_user","unban_user"} and uid == OWNER_ID:
        context.user_data.pop("state", None); context.user_data["ban_action"] = action; await q.edit_message_text("User ka numeric Telegram ID bhejein:")
    elif action == "stats" and uid == OWNER_ID:
        with db() as c:
            u=c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]; f=c.execute("SELECT COUNT(*) n FROM files").fetchone()["n"]; r=c.execute("SELECT COUNT(*) n FROM referrals").fetchone()["n"]; p=c.execute("SELECT COUNT(*) n FROM users WHERE premium=1").fetchone()["n"]
        await q.edit_message_text(f"<b>Live Stats</b>\n\nUsers: {u}\nFiles: {f}\nReferrals: {r}\nPremium Users: {p}", parse_mode="HTML", reply_markup=staff_menu(uid))
    elif action in {"add_admin","remove_admin","add_premium","remove_premium"}:
        if action in {"add_premium","remove_premium"} and allowed(uid,"premium_manage"):
            context.user_data.pop("state", None); context.user_data["premium_action"] = action; await q.edit_message_text("Premium user ka numeric Telegram ID bhejein:")
        elif action in {"add_admin","remove_admin"} and allowed(uid,"admin_manage"):
            context.user_data.pop("state", None); context.user_data["role_action"]=action; await q.edit_message_text("User ka numeric Telegram ID bhejein:")
        else: await q.edit_message_text(f"{RED} Ye option aapke role ke liye available nahi.", reply_markup=staff_menu(uid))
    elif action == "owner_back":
        await q.edit_message_text("Owner Panel", reply_markup=staff_menu(uid))
    else: await q.edit_message_text(f"{RED} Permission denied.", reply_markup=staff_menu(uid))

async def text_handler(update, context):
    if not await enforce_membership(update, context): return
    uid=update.effective_user.id; state=context.user_data.get("state"); text=update.message.text.strip()
    # Role/user-management IDs must be handled before upload, referral, or broadcast states.
    if context.user_data.get("role_action") or context.user_data.get("premium_action") or context.user_data.get("ban_action"):
        if not text.isdigit(): await update.message.reply_text("Valid numeric Telegram ID dein."); return
        if context.user_data.get("role_action"):
            role_action=context.user_data.pop("role_action")
            role = "admin" if role_action == "add_admin" else "partner" if role_action == "add_partner" else "user"
            with db() as c:
                if role_action in {"add_admin", "add_partner"}: c.execute("INSERT INTO roles(user_id,role) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET role=?", (int(text), role, role))
                else: c.execute("DELETE FROM roles WHERE user_id=? AND role=?", (int(text), "admin" if role_action == "remove_admin" else "partner"))
                c.commit()
            await save_state_now()
            notices = {"add_admin": "Aapko Admin bana diya gaya hai. Admin panel ke liye /admin command use karein.", "remove_admin": "Aapka Admin access remove kar diya gaya hai.", "add_partner": "Aapko Partner bana diya gaya hai. Partner panel ke liye /partner command use karein.", "remove_partner": "Aapka Partner access remove kar diya gaya hai."}
            try: await context.bot.send_message(int(text), notices[role_action])
            except Exception as exc: log.warning("Role notification failed: %s", exc)
            await update.message.reply_text("Role update ho gaya aur user ko notification bhej di gayi.", reply_markup=staff_menu(uid)); return
        if context.user_data.get("premium_action"):
            action = context.user_data.pop("premium_action")
            with db() as c:
                value = 1 if action == "add_premium" else 0
                c.execute("INSERT INTO users(id, username, first_name, premium) VALUES(?, '', '', ?) ON CONFLICT(id) DO UPDATE SET premium=?", (int(text), value, value)); c.commit()
            await save_state_now()
            try: await context.bot.send_message(int(text), "Aapko Premium User bana diya gaya hai." if value else "Aapka Premium access remove kar diya gaya hai.")
            except Exception as exc: log.warning("Premium notification failed: %s", exc)
            await update.message.reply_text("Premium status update ho gaya.", reply_markup=staff_menu(uid)); return
        action = context.user_data.pop("ban_action")
        value = 1 if action == "ban_user" else 0
        with db() as c:
            c.execute("INSERT INTO users(id, username, first_name, banned) VALUES(?, '', '', ?) ON CONFLICT(id) DO UPDATE SET banned=?", (int(text), value, value)); c.commit()
        await save_state_now(); await update.message.reply_text("User status update ho gaya.", reply_markup=staff_menu(uid)); return
    if state is None and text in {"🔗 Refer Link", "💰 My Balance", "👤 My Account", "💎 Contact Owner to Buy Premium"}:
        if text == "💰 My Balance":
            await update.message.reply_text(f"<b>My Balance:</b> {user_refs(uid)} referrals", parse_mode="HTML"); return
        if text == "👤 My Account":
            await update.message.reply_text(f"<b>My Account</b>\n\n<b>Status:</b> {user_status(uid)}\n<b>User ID:</b> <code>{uid}</code>\n<b>Balance:</b> {user_refs(uid)}", parse_mode="HTML"); return
        if text == "💎 Contact Owner to Buy Premium":
            if OWNER_CONTACT_URL: await update.message.reply_text("Premium purchase ke liye owner se contact karein.", reply_markup=InlineKeyboardMarkup([[button("Contact Owner", "danger", url=OWNER_CONTACT_URL)]]))
            else: await update.message.reply_text("Owner contact abhi config.json mein set nahi hai.")
            return
        me = await context.bot.get_me(); link = f"https://t.me/{me.username}?start={uid}"
        await update.message.reply_text(f"<b>Your Refer Link</b>\n\n{link}\n\nBalance: {user_refs(uid)}", parse_mode="HTML"); return
    if state==ADD_CATEGORY:
        with db() as c: c.execute("INSERT OR IGNORE INTO categories(name,created_by) VALUES(?,?)",(text,uid)); c.commit()
        if context.user_data.pop("after_category", None) == "add_file":
            rows=[[button(x['name'], callback_data=f"newfilecat:{x['id']}")] for x in categories()]
            rows.append([button("Add a new category", "success", callback_data="addfile_newcat")])
            context.user_data["state"] = ADD_FILE
            await update.message.reply_text("Category add ho gayi. Ab category select karein:", reply_markup=InlineKeyboardMarkup(rows))
        else:
            context.user_data.pop("state",None); await update.message.reply_text(f"{GREEN} Category add ho gayi.", reply_markup=staff_menu(uid))
        await save_state_now()
        return
    if state==BROADCAST:
        with db() as c: users=c.execute("SELECT id FROM users WHERE banned=0").fetchall()
        sent=0
        for u in users:
            try: await context.bot.send_message(u["id"], text); sent+=1
            except Exception: pass
        context.user_data.pop("state",None); await update.message.reply_text(f"{GREEN} Broadcast complete: {sent} users.", reply_markup=staff_menu(uid)); return
    if context.user_data.get("role_action"):
        if not text.isdigit(): await update.message.reply_text("Valid numeric user ID dein."); return
        role_action=context.user_data.pop("role_action")
        role = "admin" if role_action == "add_admin" else "partner" if role_action == "add_partner" else "user"
        with db() as c:
            if role_action in {"add_admin", "add_partner"}: c.execute("INSERT INTO roles(user_id,role) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET role=?",(int(text), role))
            else: c.execute("DELETE FROM roles WHERE user_id=? AND role=?",(int(text), "admin" if role_action == "remove_admin" else "partner"))
            c.commit()
        await save_state_now()
        notices = {"add_admin": "Aapko Admin bana diya gaya hai. Admin panel ke liye /admin command use karein.", "remove_admin": "Aapka Admin access remove kar diya gaya hai.", "add_partner": "Aapko Partner bana diya gaya hai. Partner panel ke liye /partner command use karein.", "remove_partner": "Aapka Partner access remove kar diya gaya hai."}
        try: await context.bot.send_message(int(text), notices[role_action])
        except Exception: pass
        await update.message.reply_text(f"{GREEN} Role update ho gaya aur user ko notification bhej di gayi.", reply_markup=staff_menu(uid)); return
    if context.user_data.get("ban_action"):
        if not text.isdigit(): await update.message.reply_text("Valid numeric user ID dein."); return
        action = context.user_data.pop("ban_action")
        banned = 1 if action == "ban_user" else 0
        with db() as c: c.execute("INSERT INTO users(id, username, first_name, banned) VALUES(?, '', '', ?) ON CONFLICT(id) DO UPDATE SET banned=?", (int(text), banned, banned)); c.commit()
        await save_state_now()
        try: await context.bot.send_message(int(text), "Aapka access ban kar diya gaya hai." if banned else "Aapka access restore kar diya gaya hai.")
        except Exception: pass
        await update.message.reply_text("User status update ho gaya.", reply_markup=staff_menu(uid)); return
    if context.user_data.get("premium_action"):
        if not text.isdigit(): await update.message.reply_text("Valid numeric user ID dein."); return
        action = context.user_data.pop("premium_action")
        with db() as c:
            c.execute("INSERT INTO users(id, username, first_name, premium) VALUES(?, '', '', ?) ON CONFLICT(id) DO UPDATE SET premium=?", (int(text), 1 if action == "add_premium" else 0, 1 if action == "add_premium" else 0)); c.commit()
        await save_state_now()
        try: await context.bot.send_message(int(text), "Aapko Premium User bana diya gaya hai. Ab aap referral ke baghair files access kar sakte hain." if action == "add_premium" else "Aapka Premium access remove kar diya gaya hai.")
        except Exception: pass
        await update.message.reply_text("Premium status update ho gaya aur user ko notification bhej di gayi.", reply_markup=staff_menu(uid)); return
    await update.message.reply_text("Menu se option select karein.", reply_markup=user_menu())

async def callback_router(update, context):
    if not await enforce_membership(update, context): return
    q=update.callback_query; data=q.data
    if data=="verify_gate": return await verify(update,context)
    if data=="home": return await home(update,context)
    if data=="categories": return await show_categories(update,context)
    if data.startswith("cat:"): return await show_files(update,context)
    if data.startswith("file:"): return await show_file(update,context)
    if data=="refer": return await refer(update,context)
    if data=="balance":
        await q.answer(f"Referral Balance: {user_refs(q.from_user.id)}", show_alert=True); return
    if data=="account": return await account(update, context)
    if data=="contact_owner":
        await q.answer("Owner se premium purchase ke liye direct contact karein.", show_alert=True); return
    if data.startswith("buy:"):
        fid=int(data.split(":")[1]); uid=q.from_user.id
        with db() as c:
            f=c.execute("SELECT * FROM files WHERE id=?",(fid,)).fetchone()
            premium = is_premium(uid)
            balance = c.execute("SELECT referrals FROM users WHERE id=?", (uid,)).fetchone()["referrals"]
            if not premium and balance < f["required_refs"]: await q.answer("Referrals kam hain",show_alert=True); return
            cursor = c.execute("INSERT OR IGNORE INTO purchases(user_id,file_id) VALUES(?,?)",(uid,fid))
            if cursor.rowcount and not premium and f["required_refs"]:
                c.execute("UPDATE users SET referrals=referrals-? WHERE id=?", (f["required_refs"], uid))
            c.commit()
        await save_state_now()
        await q.answer("File unlock ho gayi!"); await context.bot.send_document(uid, f["file_id"], caption=f"{GREEN} {f['name']}"); return
    if data.startswith("newfilecat:"):
        context.user_data["file_category"]=int(data.split(":")[1]); context.user_data["state"]=SET_FILE_NAME; await q.edit_message_text("Ab file upload karein (document):")
        return
    if data == "addfile_newcat":
        context.user_data["state"] = ADD_CATEGORY
        context.user_data["after_category"] = "add_file"
        await q.edit_message_text("New category ka naam bhejein:")
        return
    if data.startswith("rmcat:"):
        with db() as c: c.execute("DELETE FROM categories WHERE id=?",(int(data.split(":")[1]),)); c.commit()
        await q.edit_message_text(f"{GREEN} Category remove ho gayi.",reply_markup=staff_menu(q.from_user.id)); return
    if data.startswith("rmfile:"):
        with db() as c: c.execute("DELETE FROM files WHERE id=?",(int(data.split(":")[1]),)); c.commit()
        await q.edit_message_text(f"{GREEN} File delete ho gayi.",reply_markup=staff_menu(q.from_user.id)); return
    return await panel_action(update,context)

async def document_handler(update, context):
    if not await enforce_membership(update, context): return
    if context.user_data.get("state") != SET_FILE_NAME: return
    context.user_data["upload"]=(update.message.document.file_id, "document"); context.user_data["state"]=SET_FILE_REFS; await update.message.reply_text("File ka display name bhejein:")

async def name_handler(update, context):
    if not await enforce_membership(update, context): return
    if context.user_data.get("state") != SET_FILE_REFS: return await text_handler(update,context)
    context.user_data["file_name"]=update.message.text.strip(); context.user_data["state"]="refs"; await update.message.reply_text("Unlock ke liye kitne referrals required hain? Number bhejein:")

async def refs_handler(update, context):
    if not await enforce_membership(update, context): return
    if context.user_data.get("state") != "refs": return await text_handler(update,context)
    try: refs=int(update.message.text.strip()); assert refs>=0
    except Exception: await update.message.reply_text("Sirf positive number bhejein."); return
    fid,typ=context.user_data["upload"]; name=context.user_data["file_name"]; cid=context.user_data["file_category"]; uid=update.effective_user.id
    with db() as c:
        c.execute("INSERT INTO files(category_id,name,file_id,file_type,required_refs,created_by) VALUES(?,?,?,?,?,?)",(cid,name,fid,typ,refs,uid)); c.commit()
        users=c.execute("SELECT id FROM users WHERE banned=0").fetchall()
    for user in users:
        try: await context.bot.send_message(user["id"], f"{GREEN} New file add hui hai: {name}\\nRequired referrals: {refs}")
        except Exception: pass
    await save_state_now(); context.user_data.clear(); await update.message.reply_text(f"{GREEN} File add ho gayi aur users ko notification bhej di gayi.",reply_markup=staff_menu(uid))

async def on_error(update, context): log.exception("Unhandled error", exc_info=context.error)

def main():
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN missing. .env file configure karein.")
    if not str(GITHUB_CONFIG.get("repo", "")).strip() or not GITHUB_TOKEN:
        raise RuntimeError("github.repo aur GITHUB_TOKEN environment variable required hain.")
    init_db(); github_state_restore(); app=Application.builder().token(BOT_TOKEN).build()
    app.job_queue.run_repeating(periodic_state_sync, interval=30, first=30)
    app.add_handler(TypeHandler(Update, global_membership_guard), group=-1)
    app.add_handler(CommandHandler("start",start)); app.add_handler(CommandHandler(["owner","admin","partner"],commands))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.Document.ALL,document_handler))
    app.add_handler(MessageHandler(filters.Regex(r"^\d+$") & ~filters.COMMAND, refs_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, name_handler))
    app.add_error_handler(on_error); log.info("Shadow Files Store started"); app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=="__main__": main()
