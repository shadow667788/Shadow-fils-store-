# Shadow Files Store

Python Telegram bot with owner, admin, partner and user panels, direct category dashboard, referrals, premium users and temporary in-memory runtime storage.

## config.json

Bot settings `config.json` se load hoti hain. Is file mein do channels, **do Telegram groups**, WhatsApp link, banner URL, owner contact URL aur optional GitHub state backup configure karein.

```json
{
  "bot_token": "BOTFATHER_TOKEN",
  "owner_id": 123456789,
  "banner_url": "https://example.com/banner.jpg",
  "owner_contact_url": "https://t.me/owner_username",
  "whatsapp_channel": {"name": "WhatsApp Channel", "url": "https://whatsapp.com/channel/link"},
  "required": [
    {"type": "channel", "name": "Channel 1", "chat_id": "@channel_one", "join_url": "https://t.me/channel_one"},
    {"type": "channel", "name": "Channel 2", "chat_id": "@channel_two", "join_url": "https://t.me/channel_two"},
    {"type": "group", "name": "Group 1", "chat_id": "@group_one", "join_url": "https://t.me/group_one"},
    {"type": "group", "name": "Group 2", "chat_id": "@group_two", "join_url": "https://t.me/group_two"}
  ],
  "github": {"repo": "owner/repository", "token": "", "state_file": "bot_state.json"}
}
```

`banner_url` khali ho to welcome photo nahi bheji jayegi. `owner_contact_url` khali ho to premium contact button alert show karega.

## New user dashboard

Verification ke baad banner ke saath bold welcome message, user status, referral balance, config ki tamam categories, `Refer Link`, `My Balance`, `My Account` aur `Contact Owner to Buy Premium` buttons show hote hain.

## Premium users

Owner, admin aur partner panel mein `Add Premium` aur `Remove Premium` buttons hain. Premium user required referrals ke baghair tamam files receive kar sakta hai.

## Add File flow

`Add File` par categories ke neeche `Add a new category` button hai. Is button se category banakar foran usi flow mein file upload ki ja sakti hai.

## GitHub state backup

Agar `github.repo` configure ho aur deployment environment mein `GITHUB_TOKEN` set ho to bot startup par `bot_state.json` restore karta hai aur har 30 seconds GitHub Contents API par current state upload karta hai. `GITHUB_TOKEN` ko config ya public repository mein commit nahi karna; Railway Variables/Secrets mein set karein. Token ko fine-grained rakhein aur target repository ke liye sirf Contents read/write permission dein. Sync fail hone par owner ko error message milta hai.

## Run

```bash
python3 -m pip install -r requirements.txt
python bot.py
```

Telegram bot ko dono channels aur dono groups mein administrator bana kar member lookup permission dein. Latest button styles ke liye `python-telegram-bot 22.7` required hai.
