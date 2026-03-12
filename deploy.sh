#!/bin/bash
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#   AskMovies Public Poster Bot — One Click Deploy
#   Usage: bash deploy.sh
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_DIR="$REPO_DIR/posterbot"
SERVICE_NAME="publicposterbot"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  AskMovies Public Poster Bot — Deploy"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

echo "📦 Step 1 — Installing system packages..."
apt update -qq
apt install -y python3 python3-pip python3-venv git
echo "✅ Done"
echo ""

echo "🐍 Step 2 — Setting up virtual environment..."
cd "$BOT_DIR"
python3 -m venv venv
source venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt
echo "✅ Done"
echo ""

echo "🔍 Step 3 — Syntax check..."
python3 -c "import ast; ast.parse(open('poster_bot.py').read()); print('✅ poster_bot.py OK')"
echo ""

echo "⚙️  Step 4 — Installing systemd service..."
cat > "$SERVICE_FILE" << SVCEOF
[Unit]
Description=AskMovies Public Poster Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$BOT_DIR
ExecStart=$BOT_DIR/venv/bin/python poster_bot.py
Restart=always
RestartSec=5
TimeoutStopSec=10

Environment=BOT_TOKEN=YOUR_BOT_TOKEN_HERE
Environment=ADMIN_IDS=YOUR_TELEGRAM_USER_ID
Environment=TMDB_API_KEY=YOUR_TMDB_API_KEY
Environment=MONGO_URL=YOUR_MONGODB_URL
Environment=MONGO_DB_NAME=askfiles_public

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
echo "✅ Done"
echo ""

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ Setup complete!"
echo ""
echo "👉 NEXT — Add your credentials:"
echo "   nano $SERVICE_FILE"
echo ""
echo "   BOT_TOKEN    = token from @BotFather"
echo "   ADMIN_IDS    = your Telegram user ID (@userinfobot)"
echo "   TMDB_API_KEY = themoviedb.org/settings/api"
echo "   MONGO_URL    = MongoDB Atlas connection string"
echo ""
echo "👉 THEN START:"
echo "   systemctl daemon-reload"
echo "   systemctl start $SERVICE_NAME"
echo "   systemctl status $SERVICE_NAME"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
