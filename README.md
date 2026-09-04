# Shadow Files Store — Python Telegram Bot

Yeh project Python aur `python-telegram-bot` par based button-driven file store hai. Filhaal data **RAM-only in-memory storage** mein rakha jata hai; koi database file create nahi hoti.

## Important Telegram limitation

Telegram Bot API inline buttons ka background color bot se set nahi karne deta. Is liye `primary`, `success` ya `danger` jaisi actual button styles Telegram bot messages mein available nahi hoti. Project mein **blue/green/red emoji markers**, clear sections aur consistent button labels use kiye gaye hain. User ke Telegram theme ke mutabiq native buttons ka rang badal sakta hai, lekin bot exact RGB color force nahi kar sakta.

## Features

- Do Telegram channels aur do Telegram groups ki mandatory membership verification.
- Optional WhatsApp channel link; WhatsApp membership check nahi hoti.
- User, owner, admin aur partner panels.
- Owner: full access, categories/files, broadcasts, stats, role management ke hooks.
- Admin: category add/remove, file upload/delete aur broadcast.
- Partner: category add/remove, file upload/delete, broadcast aur admins add/remove.
- Unique referral links; referral tab count hota hai jab invitee 4 Telegram communities join karke verify karta hai.
- Required referral points par file unlock aur Telegram document delivery.
- Temporary in-memory storage, banned users ko broadcast se exclude karna.
- New file add hone par users ko automatic notification.

## Setup

```bash
cd shadow-files-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`.env` mein yeh values zaroor set karein:

```env
BOT_TOKEN=BotFather_token
OWNER_ID=apna_numeric_telegram_user_id
REQUIRED_CHANNELS=@channel_one,@channel_two
REQUIRED_GROUPS=@group_one,@group_two
WHATSAPP_CHANNEL_URL=https://wa.me/channel/optional
```

Bot ko dono channels aur dono groups mein **admin/member lookup ki permission ke liye add** karein. Private groups/channels ke liye public `@username` ke bajaye numeric chat ID ya working invite/link strategy use karein; membership API check ke liye bot ko relevant chat mein access chahiye.

Run:

```bash
python bot.py
```

## Commands

- `/start` — membership gate ke baad user panel.
- `/owner` — sirf configured owner.
- `/admin` — assigned admin.
- `/partner` — assigned partner.

Normal users ko protected commands par permission-denied message milta hai.

## File upload flow

Owner/admin/partner panel mein `Add File` dabayein, category choose karein, Telegram document upload karein, display name bhejein aur required referrals ka number bhejein. File database mein save hoti hai, users ko notification jati hai, aur eligible user purchase button se document receive karta hai.

## Railway deployment

Railway variables mein `BOT_TOKEN`, `OWNER_ID`, `REQUIRED_CHANNELS` aur `REQUIRED_GROUPS` set karein. Bot ab `sqlite3` ki koi file open/create nahi karta, is liye `unable to open database file` startup error nahi aayega. Railway restart/redeploy hone par users, categories, files aur roles reset ho jayenge. Aap jab database details denge to persistent storage dobara add ki ja sakti hai.

## Production run

Bot ko 24/7 chalane ke liye VPS, Docker, systemd, ya kisi persistent Python host par run karein. Long polling ke liye ek waqt mein sirf ek bot process chalna chahiye. Token ko kabhi GitHub par commit na karein.

## Next hardening before public launch

1. Owner-only partner add/remove aur full ban/unban workflows ko production policy ke mutabiq enable karein.
2. Broadcast rate limiting aur retry queue add karein.
3. Files ko Telegram `file_id` ke saath optional cloud backup mein replicate karein.
4. Database decide hone ke baad in-memory storage ko persistent database se replace karein.
