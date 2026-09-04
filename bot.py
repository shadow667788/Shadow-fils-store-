import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)

load_dotenv()
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("shadow-files")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
try:
    OWNER_ID = int(os.getenv("OWNER_ID", "0").strip() or "0")
except ValueError:
    OWNER_ID = 0


DB_PATH = ":memory:"
MEMORY_CONNECTION = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
MEMORY_CONNECTION.row_factory = sqlite3.Row
MEMORY_CONNECTION.execute("PRAGMA foreign_keys = ON")
WA_URL = os.getenv("WHATSAPP_CHANNEL_URL", "").strip()
CHANNELS = [x.strip() for x in os.getenv("REQUIRED_CHANNELS", "").split(",") if x.strip()]
GROUPS = [x.strip() for x in os.getenv("REQUIRED_GROUPS", "").split(",") if x.strip()]

ADD_CATEGORY, ADD_FILE, SET_FILE_NAME, SET_FILE_REFS, BROADCAST = range(5)

# Telegram's Bot API does not expose arbitrary button background colors. The UI uses
# colored-looking emoji markers (blue, green, red) and clear visual sections instead.
BLUE = "🔵"
GREEN = "🟢"
RED = "🔴"
GOLD = "🟡"


def db():
    return MEMORY_CONNECTION


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, referrals INTEGER NOT NULL DEFAULT 0,
            banned INTEGER NOT NULL DEFAULT 0, joined_gate INTEGER NOT NULL DEFAULT 0,
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
    matrix = {"category": {"owner", "admin", "partner"}, "file": {"owner", "admin", "partner"}, "broadcast": {"owner", "admin", "partner"}, "admin_manage": {"owner", "partner"}, "user_manage": {"owner"}}
    return role in matrix.get(action, set())


def categories():
    with db() as c: return c.execute("SELECT * FROM categories ORDER BY id DESC").fetchall()

def files_for(cid):
    with db() as c: return c.execute("SELECT * FROM files WHERE category_id=? ORDER BY id DESC", (cid,)).fetchall()

def user_refs(uid):
    with db() as c: return c.execute("SELECT referrals FROM users WHERE id=?", (uid,)).fetchone()["referrals"]


def gate_keyboard():
    rows = []
    for i, chat in enumerate(CHANNELS + GROUPS):
        label = f"{BLUE} Join Telegram {'Channel' if i < len(CHANNELS) else 'Group'} {i+1}"
        # For usernames, t.me link is convenient. Numeric/private IDs need an invite link configured in Telegram.
        link = chat if chat.startswith("http") else f"https://t.me/{chat.lstrip('@')}"
        rows.append([InlineKeyboardButton(label, url=link)])
    if WA_URL: rows.append([InlineKeyboardButton(f"🟢 Optional WhatsApp Channel", url=WA_URL)])
    rows.append([InlineKeyboardButton(f"{GREEN} Verify Telegram Membership", callback_data="verify_gate")])
    return InlineKeyboardMarkup(rows)

async def check_gate(bot, uid: int) -> bool:
    if not CHANNELS and not GROUPS: return True
    for chat in CHANNELS + GROUPS:
        try:
            m = await bot.get_chat_member(chat, uid)
            if m.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
                return False
        except Exception as e:
            log.warning("Gate check failed for %s/%s: %s", chat, uid, e)
            return False
    return True

async def gate_or_prompt(update, context) -> bool:
    uid = update.effective_user.id
    if await check_gate(context.bot, uid):
        with db() as c:
            c.execute("UPDATE users SET joined_gate=1 WHERE id=?", (uid,)); c.commit()
        return True
    text = f"{RED} Access band hai. Pehle dono Telegram channels aur dono groups join karein, phir Verify dabayein.\n\nWhatsApp link optional hai."
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=gate_keyboard())
    else:
        await update.effective_message.reply_text(text, reply_markup=gate_keyboard())
    return False


def user_menu():
    rows = [[InlineKeyboardButton(f"{BLUE} Categories", callback_data="categories"), InlineKeyboardButton(f"{GREEN} Refer & Earn", callback_data="refer")]]
    return InlineKeyboardMarkup(rows)

def staff_menu(uid):
    role = get_role(uid)
    rows = [[InlineKeyboardButton(f"{BLUE} Add Category", callback_data="add_category"), InlineKeyboardButton(f"{RED} Remove Category", callback_data="remove_category")], [InlineKeyboardButton(f"{GREEN} Add File", callback_data="add_file"), InlineKeyboardButton(f"{RED} Delete File", callback_data="delete_file")], [InlineKeyboardButton(f"{GOLD} Broadcast", callback_data="broadcast")]]
    if role in {"owner", "partner"}: rows.append([InlineKeyboardButton(f"{BLUE} Add Admin", callback_data="add_admin"), InlineKeyboardButton(f"{RED} Remove Admin", callback_data="remove_admin")])
    if role == "owner": rows.extend([[InlineKeyboardButton(f"{GREEN} Manage Partners", callback_data="partners")], [InlineKeyboardButton(f"{RED} Ban/Unban User", callback_data="ban")], [InlineKeyboardButton(f"{GOLD} Live Stats", callback_data="stats")]])
    rows.append([InlineKeyboardButton("⬅️ User Panel", callback_data="home")])
    return InlineKeyboardMarkup(rows)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; upsert_user(u)
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
                try: await context.bot.send_message(ref, f"{GREEN} Aapka referral verify ho gaya! +1 point.")
                except Exception: pass
    await update.effective_message.reply_text(f"{GREEN} Welcome to Shadow Files Store!\n\nApna panel select karein:", reply_markup=user_menu())

async def verify(update, context):
    q = update.callback_query; await q.answer()
    if await check_gate(context.bot, q.from_user.id):
        with db() as c: c.execute("UPDATE users SET joined_gate=1 WHERE id=?", (q.from_user.id,)); c.commit()
        await q.edit_message_text(f"{GREEN} Verification successful! Welcome.", reply_markup=user_menu())
    else: await q.edit_message_text(f"{RED} Kuch channels/groups abhi join nahi hue. Join karke dobara verify karein.", reply_markup=gate_keyboard())

async def commands(update, context):
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
    rows=[[InlineKeyboardButton(f"📁 {x['name']}", callback_data=f"cat:{x['id']}")] for x in categories()]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
    await q.edit_message_text("📁 Categories select karein:", reply_markup=InlineKeyboardMarkup(rows))

async def show_files(update, context):
    q=update.callback_query; await q.answer(); cid=int(q.data.split(":")[1]); rows=[[InlineKeyboardButton(f"📄 {x['name']} — {x['required_refs']} refs", callback_data=f"file:{x['id']}")] for x in files_for(cid)]
    rows.append([InlineKeyboardButton("⬅️ Categories", callback_data="categories")]); await q.edit_message_text("📄 Files:", reply_markup=InlineKeyboardMarkup(rows))

async def show_file(update, context):
    q=update.callback_query; await q.answer(); fid=int(q.data.split(":")[1]); uid=q.from_user.id
    with db() as c:
        f=c.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone(); bought=c.execute("SELECT 1 FROM purchases WHERE user_id=? AND file_id=?", (uid,fid)).fetchone()
    if not f: return
    if bought or user_refs(uid) >= f["required_refs"]:
        await q.edit_message_text(f"{GREEN} {f['name']} ready hai. Purchase karke file receive karein.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{GREEN} Purchase / Get File", callback_data=f"buy:{fid}")],[InlineKeyboardButton("⬅️ Files", callback_data=f"cat:{f['category_id']}")]]))
    else:
        await q.edit_message_text(f"{RED} Ye file locked hai.\nRequired referrals: {f['required_refs']}\nAapke referrals: {user_refs(uid)}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{BLUE} Refer Link", callback_data="refer")],[InlineKeyboardButton("⬅️ Files", callback_data=f"cat:{f['category_id']}")]]))

async def refer(update, context):
    q=update.callback_query; await q.answer(); me=await context.bot.get_me(); link=f"https://t.me/{me.username}?start={q.from_user.id}"; await q.edit_message_text(f"{BLUE} Aapka unique referral link:\n\n{link}\n\nDost ko link bhejein. Referral tab count hoga jab wo 4 Telegram communities join karke verify kare.\n\nCurrent points: {user_refs(q.from_user.id)}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Home", callback_data="home")]]))

async def panel_action(update, context):
    q=update.callback_query; await q.answer(); uid=q.from_user.id; action=q.data
    if action in {"add_category","remove_category"} and allowed(uid,"category"):
        if action=="add_category": context.user_data["state"]=ADD_CATEGORY; await q.edit_message_text("Category ka naam bhejein:")
        else:
            rows=[[InlineKeyboardButton(f"{RED} {x['name']}", callback_data=f"rmcat:{x['id']}")] for x in categories()]; await q.edit_message_text("Remove karne wali category select karein:", reply_markup=InlineKeyboardMarkup(rows))
    elif action in {"add_file","delete_file"} and allowed(uid,"file"):
        if action=="add_file":
            rows=[[InlineKeyboardButton(x['name'], callback_data=f"newfilecat:{x['id']}")] for x in categories()]; context.user_data["state"]=ADD_FILE; await q.edit_message_text("File kis category mein add karni hai?", reply_markup=InlineKeyboardMarkup(rows))
        else:
            rows=[[InlineKeyboardButton(f"📄 {x['name']}", callback_data=f"rmfile:{x['id']}")] for cat in categories() for x in files_for(cat['id'])]; await q.edit_message_text("Delete karne wali file:", reply_markup=InlineKeyboardMarkup(rows))
    elif action=="broadcast" and allowed(uid,"broadcast"): context.user_data["state"]=BROADCAST; await q.edit_message_text("Broadcast message bhejein:")
    elif action in {"add_admin","remove_admin","partners","ban","stats"}:
        if action in {"add_admin","remove_admin"} and allowed(uid,"admin_manage"): context.user_data["role_action"]=action; await q.edit_message_text("User ka numeric Telegram ID bhejein:")
        elif action=="stats" and uid==OWNER_ID:
            with db() as c: u=c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]; f=c.execute("SELECT COUNT(*) n FROM files").fetchone()["n"]; await q.edit_message_text(f"{GOLD} Stats\nUsers: {u}\nFiles: {f}", reply_markup=staff_menu(uid))
        else: await q.edit_message_text(f"{RED} Ye option aapke role ke liye available nahi.", reply_markup=staff_menu(uid))
    else: await q.edit_message_text(f"{RED} Permission denied.", reply_markup=staff_menu(uid))

async def text_handler(update, context):
    uid=update.effective_user.id; state=context.user_data.get("state"); text=update.message.text.strip()
    if state==ADD_CATEGORY:
        with db() as c: c.execute("INSERT OR IGNORE INTO categories(name,created_by) VALUES(?,?)",(text,uid)); c.commit()
        context.user_data.pop("state",None); await update.message.reply_text(f"{GREEN} Category add ho gayi.", reply_markup=staff_menu(uid)); return
    if state==BROADCAST:
        with db() as c: users=c.execute("SELECT id FROM users WHERE banned=0").fetchall()
        sent=0
        for u in users:
            try: await context.bot.send_message(u["id"], text); sent+=1
            except Exception: pass
        context.user_data.pop("state",None); await update.message.reply_text(f"{GREEN} Broadcast complete: {sent} users.", reply_markup=staff_menu(uid)); return
    if context.user_data.get("role_action"):
        if not text.isdigit(): await update.message.reply_text("Valid numeric user ID dein."); return
        role_action=context.user_data.pop("role_action"); role="admin" if role_action=="add_admin" else "user"
        with db() as c:
            if role_action=="add_admin": c.execute("INSERT INTO roles(user_id,role) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET role='admin'",(int(text),))
            else: c.execute("DELETE FROM roles WHERE user_id=? AND role='admin'",(int(text),))
            c.commit()
        await update.message.reply_text(f"{GREEN} Role update ho gaya.", reply_markup=staff_menu(uid)); return
    await update.message.reply_text("Menu se option select karein.", reply_markup=user_menu())

async def callback_router(update, context):
    q=update.callback_query; data=q.data
    if data=="verify_gate": return await verify(update,context)
    if data=="home": return await home(update,context)
    if data=="categories": return await show_categories(update,context)
    if data.startswith("cat:"): return await show_files(update,context)
    if data.startswith("file:"): return await show_file(update,context)
    if data=="refer": return await refer(update,context)
    if data.startswith("buy:"):
        fid=int(data.split(":")[1]); uid=q.from_user.id
        with db() as c:
            f=c.execute("SELECT * FROM files WHERE id=?",(fid,)).fetchone()
            if user_refs(uid)<f["required_refs"]: await q.answer("Referrals kam hain",show_alert=True); return
            c.execute("INSERT OR IGNORE INTO purchases(user_id,file_id) VALUES(?,?)",(uid,fid)); c.commit()
        await q.answer("File unlock ho gayi!"); await context.bot.send_document(uid, f["file_id"], caption=f"{GREEN} {f['name']}"); return
    if data.startswith("newfilecat:"):
        context.user_data["file_category"]=int(data.split(":")[1]); context.user_data["state"]=SET_FILE_NAME; await q.edit_message_text("Ab file upload karein (document):")
        return
    if data.startswith("rmcat:"):
        with db() as c: c.execute("DELETE FROM categories WHERE id=?",(int(data.split(":")[1]),)); c.commit()
        await q.edit_message_text(f"{GREEN} Category remove ho gayi.",reply_markup=staff_menu(q.from_user.id)); return
    if data.startswith("rmfile:"):
        with db() as c: c.execute("DELETE FROM files WHERE id=?",(int(data.split(":")[1]),)); c.commit()
        await q.edit_message_text(f"{GREEN} File delete ho gayi.",reply_markup=staff_menu(q.from_user.id)); return
    return await panel_action(update,context)

async def document_handler(update, context):
    if context.user_data.get("state") != SET_FILE_NAME: return
    context.user_data["upload"]=(update.message.document.file_id, "document"); context.user_data["state"]=SET_FILE_REFS; await update.message.reply_text("File ka display name bhejein:")

async def name_handler(update, context):
    if context.user_data.get("state") != SET_FILE_REFS: return await text_handler(update,context)
    context.user_data["file_name"]=update.message.text.strip(); context.user_data["state"]="refs"; await update.message.reply_text("Unlock ke liye kitne referrals required hain? Number bhejein:")

async def refs_handler(update, context):
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
    context.user_data.clear(); await update.message.reply_text(f"{GREEN} File add ho gayi aur users ko notification bhej di gayi.",reply_markup=staff_menu(uid))

async def on_error(update, context): log.exception("Unhandled error", exc_info=context.error)

def main():
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN missing. .env file configure karein.")
    init_db(); app=Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",start)); app.add_handler(CommandHandler(["owner","admin","partner"],commands))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.Document.ALL,document_handler))
    app.add_handler(MessageHandler(filters.Regex(r"^\d+$") & ~filters.COMMAND, refs_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, name_handler))
    app.add_error_handler(on_error); log.info("Shadow Files Store started"); app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=="__main__": main()
