# Shadow Files Store

Python Telegram bot with user, owner, admin and partner panels, referrals, file unlocking and temporary RAM-only storage. No database file is created.

## Configuration

Bot settings ab `.env` ya Railway variables se nahi, **`config.js`** se li jati hain. Is file mein apna token, owner ID, do channels, ek group aur WhatsApp link set karein:

```json
{
  "bot_token": "BOTFATHER_TOKEN",
  "owner_id": 123456789,
  "whatsapp_channel": {
    "name": "WhatsApp Channel",
    "url": "https://wa.me/channel/your_link"
  },
  "required": [
    {"type": "channel", "name": "Channel 1", "chat_id": "@channel_one", "join_url": "https://t.me/channel_one"},
    {"type": "channel", "name": "Channel 2", "chat_id": "@channel_two", "join_url": "https://t.me/channel_two"},
    {"type": "group", "name": "Main Group", "chat_id": "@main_group", "join_url": "https://t.me/main_group"}
  ]
}
```

`chat_id` mein public username `@channelname` ke saath likhein. Bot ko dono channels aur ek group mein administrator bana kar member lookup ki permission dein.

## Membership flow

User ko neeche diye gaye tamam WhatsApp aur Telegram channels/groups join karne honge. WhatsApp link display hota hai lekin uski membership API se check nahi hoti. Telegram verification mein bot har configured Telegram chat ko check karta hai. Agar koi chat fail ho to user ko us chat ka exact configured `name` dikhaya jata hai.

## Run

```bash
python3 -m pip install -r requirements.txt
python bot.py
```

`config.js` ko kabhi public repository mein real token ke saath commit na karein. Is repository mein placeholder config rakhein aur deployment platform par secret-safe file/config method use karein.

## Button styles

Latest `python-telegram-bot` 22.7 use hota hai. Telegram-native styles `success` (green), `primary` (blue) aur `danger` (red) use kiye gaye hain. Join buttons green, blue, red sequence mein cycle karte hain.

## Temporary storage

Data sirf process memory mein hai. Restart ya redeploy ke baad users, roles, categories, files aur referrals reset ho jayenge. Persistent database baad mein add ki ja sakti hai.
