#!/usr/bin/env bash
# VM（Ubuntu）で Bot をセットアップします。何度実行しても安全です。Bot は開始しません（確認してから開始します）。
#
# 使い方（VM の SSH 画面で、リポジトリを clone した後）:
#     cd ~/life-os-bot && bash deploy/setup_vm.sh
#
# 事前に ~/life-os-bot/.env と ~/life-os-bot/credentials.json を置いておいてください。
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=life-os-bot

if [ "$(id -u)" -eq 0 ]; then
  echo "root ではなく、普通のユーザーで実行してください（sudo は必要な所だけ内部で使います）。" >&2
  exit 1
fi
cd "$APP_DIR"

echo "== 1/6 必要なものをインストール =="
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git

PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10 以上が必要です。Ubuntu 22.04 か 24.04 を使ってください。")
PY
echo "Python $PYV"

echo "== 2/6 メモリ不足対策のスワップ（e2-micro はメモリ 1GB のため） =="
if [ "$(swapon --show --noheadings | wc -l)" -eq 0 ]; then
  sudo fallocate -l 1G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  echo "1GB のスワップを作成しました"
else
  echo "スワップは設定済みです"
fi

echo "== 3/6 秘密ファイルの確認 =="
for f in .env credentials.json; do
  if [ ! -f "$f" ]; then
    echo "$APP_DIR/$f がありません。手順書のとおり、SSH 画面からアップロードして配置してください。" >&2
    exit 1
  fi
  chmod 600 "$f"
done
if grep -q '^USE_TRUSTSTORE=1' .env; then
  echo "警告: .env に USE_TRUSTSTORE=1 があります（開発用パソコン専用の設定です）。VM では削除してください。" >&2
fi
missing=""
for k in DISCORD_TOKEN ANTHROPIC_API_KEY GOOGLE_SHEET_ID GAS_RELAY_URL GAS_RELAY_TOKEN OBSIDIAN_LIFEOS_FOLDER_ID; do
  grep -Eq "^${k}=.+" .env || missing="$missing $k"
done
if [ -n "$missing" ]; then
  echo ".env に未入力の項目があります:$missing" >&2
  exit 1
fi
echo ".env と credentials.json を確認しました（権限を 600 にしました）"

echo "== 4/6 Python の環境とライブラリ =="
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

echo "== 5/6 常時起動の設定（systemd） =="
sed "s/__USER__/$(id -un)/g" deploy/life-os-bot.service | sudo tee /etc/systemd/system/${SERVICE}.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE"

echo "== 6/6 完了 =="
cat <<MSG

セットアップが終わりました。Bot はまだ開始していません。次の順に進めてください。

  1) 接続の確認（Discord / Claude / シート）:
       cd $APP_DIR && .venv/bin/python scripts/check_connections.py
  2) Obsidian 連携（GAS 中継）の確認:
       .venv/bin/python scripts/verify_relay.py
  3) パソコンの Bot を停止しているか確認してから、開始:
       sudo systemctl start $SERVICE
       sudo systemctl status $SERVICE --no-pager
  4) ログの確認（止めるときは Ctrl+C）:
       sudo journalctl -u $SERVICE -f

MSG
