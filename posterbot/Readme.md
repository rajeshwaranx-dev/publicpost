# 🎬 AskMovies Public Poster Bot

Multi-user Telegram auto-post bot. Admin manages all users. Each user gets their own log channels, public channels, filestore bot, custom header, join text and caption.

---

## ⚡ Deploy in 1 Minute

```bash
# 1 — Clone
git clone https://github.com/rajeshwaranx-dev/public-upload.git
cd public-upload

# 2 — Deploy
bash deploy.sh

# 3 — Add credentials
nano /etc/systemd/system/publicposterbot.service

# 4 — Start
systemctl daemon-reload
systemctl start publicposterbot
systemctl status publicposterbot
```

---

## 🔑 Credentials Needed

| Variable | Where to Get |
|---|---|
| `BOT_TOKEN` | @BotFather → /newbot |
| `ADMIN_IDS` | @userinfobot → your user ID |
| `TMDB_API_KEY` | themoviedb.org/settings/api (free) |
| `MONGO_URL` | MongoDB Atlas → Connect → Drivers |
| `MONGO_DB_NAME` | Any name, default: askfiles_public |

---

## 📋 Admin Commands

### User Management
```
/adduser name filestore_bot     — Add new user
/removeuser name                — Delete user
/listusers                      — Show all users
/userinfo name                  — Show user details
/toggleuser name                — Activate/deactivate
```

### User Config
```
/setlog name -100xxx            — Add log channel (max 2)
/removelog name -100xxx         — Remove log channel
/setchannel name -100xxx        — Add public channel (max 3)
/removechannel name -100xxx     — Remove public channel
/setfilestore name BotUsername  — Set filestore bot
/setworker name https://...     — Set Cloudflare worker URL
```

### Caption Customization
```
/setheader name Text | https://link   — Set top header per user
/getheader name                       — View header
/removeheader name                    — Reset to default

/setjoin name ❤️Join @Chan\n📢 @Chan2  — Set bottom join line(s)
/getjoin name                          — View join text
/removejoin name                       — Reset to default

/setcaption name template             — Set full custom caption
/resetcaption name                    — Reset to default
```

### Bot Control
```
/poster on|off      — Toggle TMDB poster
/rating on|off      — Toggle TMDB rating
/pause / /resume    — Pause/resume posting
/stats              — Global stats
/failed             — Failed posts
/retry              — Retry failed
/commands           — All commands
```

---

## 📝 Caption Placeholders
```
{header}        — Top hyperlink line
{title}         — Movie title
{year}          — Release year
{quality}       — WEB-DL / HDRip
{audio}         — Tamil + Telugu etc
{season}        — Season (series only)
{rating}        — ⭐ 8.2/10
{files}         — Download links
{batch}         — Get all files link
{join}          — Bottom join line
{filestore_bot} — Their bot username
```

---

## 🚀 Add First User
```
/adduser john JohnFilestoreBot
/setlog john -100xxxxxxxxxx
/setchannel john -100xxxxxxxxxx
/setheader john JohnMovies | https://t.me/JohnChannel
/setjoin john ❤️Join » @JohnChannel
/userinfo john
```

---

## 🛠 Server Commands
```bash
systemctl start publicposterbot
systemctl stop publicposterbot
systemctl restart publicposterbot
systemctl status publicposterbot
journalctl -u publicposterbot -f
journalctl -u publicposterbot -n 50 --no-pager
```

---
Powered By ❤️ @Master_xkid
