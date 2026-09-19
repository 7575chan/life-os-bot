#!/usr/bin/env bash
# コードを更新して Bot を再起動します。パソコンで修正 → GitHub に push した後、VM の SSH 画面で実行します。
#     cd ~/life-os-bot && bash deploy/update.sh
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

git pull --ff-only
.venv/bin/python -m pip install -r requirements.txt
sudo systemctl restart life-os-bot
sleep 8
sudo systemctl status life-os-bot --no-pager | head -12
echo
echo "ログの確認: sudo journalctl -u life-os-bot -n 30 --no-pager"
