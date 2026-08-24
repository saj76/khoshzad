#!/usr/bin/env bash
# Install or update the personal weekly-bot as a systemd --user service. Re-runnable.
set -euo pipefail
BOT="$HOME/weekly-bot"
CFG="$HOME/.config/weekly-bot"
UNIT_DIR="$HOME/.config/systemd/user"

# No python3-venv/ensurepip on this box (and no sudo): uv makes the venv; pip --user is the fallback.
if command -v uv >/dev/null 2>&1; then
  uv venv -q "$BOT/.venv"
  uv pip install -q --python "$BOT/.venv/bin/python" "discord.py>=2.3"
else
  python3 -m pip install -q --user "discord.py>=2.3"
  mkdir -p "$BOT/.venv/bin" && ln -sf "$(command -v python3)" "$BOT/.venv/bin/python"
fi

mkdir -p "$CFG" "$UNIT_DIR"
if [ ! -f "$CFG/.env" ]; then
  cp "$BOT/env.example" "$CFG/.env"
  chmod 600 "$CFG/.env"
  echo "wrote $CFG/.env from the example — fill DISCORD_BOT_TOKEN, OWNER_ID, CHANNEL_ID before starting"
fi
chmod 600 "$CFG/.env"

cp "$BOT/weekly-bot.service" "$UNIT_DIR/weekly-bot.service"
systemctl --user daemon-reload
systemctl --user enable weekly-bot.service >/dev/null

if grep -qE '^DISCORD_BOT_TOKEN=.{20,}' "$CFG/.env"; then
  systemctl --user restart weekly-bot.service
  sleep 2
  systemctl --user --no-pager --lines=5 status weekly-bot.service || true
else
  echo "token not set yet — start later with: systemctl --user start weekly-bot"
fi
echo "logs: journalctl --user -u weekly-bot -f"
