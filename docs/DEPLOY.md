# 24時間動かす手順（マニュアル 第8章の私専用版）

パソコンを閉じても、朝 8:00 と夜 21:00 の投稿と、Discord への返信が続くようにします。
流れは、マニュアルの第8章（9-1〜9-6）と同じです。違うところは **★** で示します。

## 全体像

```
パソコン ──(GitHub に push)──▶ GitHub（非公開） ──(VM が pull)──▶ GCP の VM（24時間動く）
   │                                                                   ▲
   └── .env と credentials.json は GitHub に上げず、SSH 画面から VM に直接置く ─┘
```

- GitHub に上がるのは、コードだけです。`.env`、`credentials.json`、`data/`、執筆記録、個人の設計資料は、`.gitignore` で除外済みです。
- ★ マニュアルは `credentials.json` を `.env` の1行に変換しますが、この Bot では**ファイルのまま**置きます（コードの変更が要らず、間違えにくいため）。

## どこまで終わっていますか？（再開する場所）

| 済んでいること | 次にやること |
|---|---|
| 何もしていない | 手順 0 から |
| GitHub のリポジトリを作った（空） | 手順 1-2 から |
| コードを GitHub に上げた | 手順 2 から |
| GCP の VM を作った | 手順 3 から |
| VM に SSH で入れる | 手順 3（コードの取得）から |

## 手順 0: パソコンの Bot を止める

パソコンで Bot を動かしているときは、そのウィンドウで **Ctrl+C** を押して止めます。
（VM と同時に動いても、二重起動防止で片方は起動しませんが、止めてから切り替えるのが安全です）

## 手順 1: GitHub にコードを上げる（マニュアル 9-1）

### 1-1. 空のリポジトリを作る
1. github.com にログイン → 右上の「＋」→「New repository」
2. Repository name: `life-os-bot`、**Private** を選ぶ
3. 「Add a README」「.gitignore」「license」は**すべてオフ**のまま「Create repository」

### 1-2. パソコンから push する
`life-os-bot` フォルダで PowerShell を開き、次を1行ずつ実行します（`あなたのユーザー名` は自分のものに）。
`git init` は済ませてあります。

```powershell
# 何が上がるかを確認（.env / credentials.json / vm.env / data が出てこなければ OK）
git status --short

git add .
git commit -m "初回のコミット"
git remote add origin https://github.com/あなたのユーザー名/life-os-bot.git
git push -u origin main
```

- `git commit` で「名前とメールを設定してください」と出たら、`git config --global user.name "あなたの名前"` と `git config --global user.email "あなたのメール"` を実行してからやり直します。
- `git push` でブラウザが開いたら、GitHub にログインして許可します（**★ トークンを作ってチャットやコマンドに貼る必要はありません**。Git for Windows の認証機能が使われます）。
- 確認: GitHub のリポジトリのページに `main.py` などが表示されれば成功です。`.env` や `credentials.json` が**表示されていないこと**も確認してください。

## 手順 2: GCP で VM を作る（マニュアル 9-2）

1. console.cloud.google.com にログイン（サービスアカウントを作ったのと同じ Google Cloud のプロジェクトで構いません）
2. 左メニュー「Compute Engine」→「VM インスタンス」→（初回は「有効にする」）→「インスタンスを作成」
3. 次のとおりに設定します。

| 項目 | 設定 |
|---|---|
| 名前 | `life-os-bot` |
| リージョン | `us-central1`（無料枠の対象） |
| マシンタイプ | `e2-micro`（無料枠） |
| ブートディスク | 「OSとストレージ」→「変更」→ **Ubuntu 24.04 LTS**（x86/64, amd64）。★マニュアルは 22.04 ですが、どちらでも動きます。24.04 の方が新しく、長くサポートされます |
| ディスク | 標準永続ディスク 30GB 以内（無料枠） |

4. 「作成」。右側の「月間予測 $7」は定価の表示で、無料枠の範囲なら請求は $0 です（マニュアルの注意書きのとおり）。
5. 一覧に `life-os-bot` が出たら、右側の「SSH」ボタンを押します。ブラウザに黒い画面（VM の中）が開きます。

## 手順 3: VM にコードを取得する（マニュアル 9-4）

SSH 画面で、次を実行します。

```bash
sudo apt-get update && sudo apt-get install -y git
```

### 3-A. ★ 読み取り専用の「デプロイキー」で取得する（おすすめ）
マニュアルの方法（強い権限のトークンを URL に埋め込む）より安全です。この VM は、このリポジトリを**読むだけ**になります。

```bash
ssh-keygen -t ed25519 -C "life-os-bot-vm" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub
```

表示された1行（`ssh-ed25519 …` で始まる）をコピーして、GitHub のリポジトリ →「Settings」→「Deploy keys」→「Add deploy key」に貼ります。Title は `life-os-bot-vm`、**「Allow write access」はオフ**のまま「Add key」。

```bash
git clone git@github.com:あなたのユーザー名/life-os-bot.git
```

初回だけ「Are you sure you want to continue connecting?」と聞かれたら `yes` と入力します。

### 3-B. マニュアルどおり、トークンで取得する
GitHub の「Settings」→「Developer settings」→「Personal access tokens」→「Fine-grained tokens」で、**このリポジトリだけ**、権限は **Contents: Read-only** のトークンを作ります（マニュアルの「classic・repo・No expiration」は、すべての非公開リポジトリを操作できてしまうため、おすすめしません）。

```bash
git clone https://あなたのユーザー名:トークン@github.com/あなたのユーザー名/life-os-bot.git
```

### 確認
```bash
ls ~/life-os-bot
```
`main.py` や `requirements.txt` が表示されれば成功です。

## 手順 4: `.env` と `credentials.json` を VM に置く（マニュアル 9-5）

### 4-1. パソコンで、VM 用の `.env` を作る
```powershell
.venv\Scripts\python scripts\make_vm_env.py
```
`deploy\vm.env` ができます。開発用パソコン専用の設定（`USE_TRUSTSTORE` など）が取り除かれ、必要な項目が入力済みかどうかが、項目名だけで表示されます（値は表示されません）。「★未入力の項目」が出たら、パソコンの `.env` に入力してからやり直します。

### 4-2. SSH 画面から、2つのファイルをアップロードする
SSH 画面の右上の歯車（⚙）→「ファイルをアップロード」で、次の2つを選びます。
- `life-os-bot\deploy\vm.env`
- `life-os-bot\credentials.json`

### 4-3. 正しい場所に置く
```bash
mv ~/vm.env ~/life-os-bot/.env
mv ~/credentials.json ~/life-os-bot/credentials.json
chmod 600 ~/life-os-bot/.env ~/life-os-bot/credentials.json
ls -l ~/life-os-bot/.env ~/life-os-bot/credentials.json
```
2つのファイルが表示されれば OK です（「No such file or directory」ならアップロードをやり直します）。

> アップロードが終わったら、パソコンの `deploy\vm.env` は削除して構いません。

## 手順 5: セットアップスクリプトを実行する（マニュアル 9-4 と 9-6 の一部）

```bash
cd ~/life-os-bot
bash deploy/setup_vm.sh
```

これで、次のことが自動で行われます（何度実行しても安全です）。
- Python などのインストール、メモリ不足対策のスワップ（1GB）の作成
- `.env` と `credentials.json` の存在と入力漏れの確認
- ライブラリのインストール（★マニュアルの `--break-system-packages` は使わず、専用の環境 `.venv` に入れます）
- 常時起動の設定（systemd）。**Bot はまだ開始しません**

## 手順 6: 開始する前の確認

```bash
cd ~/life-os-bot
.venv/bin/python scripts/check_connections.py     # Discord / Claude / 2つのシート
.venv/bin/python scripts/verify_relay.py          # Obsidian 連携（12項目）
```
どちらも全項目が成功することを確認します。

## 手順 7: 開始する（マニュアル 9-6）

**パソコンの Bot が止まっていることを確認してから**、開始します。

```bash
sudo systemctl start life-os-bot
sudo systemctl status life-os-bot --no-pager
```
`active (running)` と出れば成功です。ログは次で見られます（止めるときは Ctrl+C）。

```bash
sudo journalctl -u life-os-bot -f
```
`logged in as 人生管理OS-BOT` が出れば、Discord に接続できています。

**最終確認**: パソコンを閉じた状態で、`01-today-task` にメッセージを送り、Bot が返信してくれば成功です。翌朝 8:00 に、朝の案内が自動で届きます。

## 運用

| やりたいこと | コマンド（VM の SSH 画面） |
|---|---|
| ログを見る | `sudo journalctl -u life-os-bot -f` |
| 状態を見る | `sudo systemctl status life-os-bot --no-pager` |
| 停止 | `sudo systemctl stop life-os-bot` |
| 再起動 | `sudo systemctl restart life-os-bot` |
| コードを更新（パソコンで直して push した後） | `cd ~/life-os-bot && bash deploy/update.sh`（Bot を再起動します）。**まだ手順 7 の前（Bot を開始していない）なら**、再起動せずに `cd ~/life-os-bot && git pull --ff-only` だけにします |
| `.env` を直した後 | `sudo systemctl restart life-os-bot` |

- **VM が再起動しても**、Bot は自動で起動します（`enable` 済み）。
- **Bot が異常終了しても**、15 秒後に自動で再起動します。
- **二重起動の防止**: パソコンで Bot を動かすと、VM の Bot が動いている間は「すでに別の場所で Bot が動いています」と表示して起動しません。パソコンで試したいときは、先に VM の Bot を停止します（`sudo systemctl stop life-os-bot`）。終わったら、VM で `sudo systemctl start life-os-bot` します。
- 朝・夜の投稿は、別の場所がその日にすでに投稿していれば、重複して投稿しません。

## うまくいかないとき

| 症状 | 確認すること |
|---|---|
| `setup_vm.sh` が「.env に未入力の項目があります」と言う | パソコンの `.env` を直して、手順 4 をやり直す |
| `check_connections.py` の Discord が失敗 | `.env` の `DISCORD_TOKEN`、Developer Portal の「Message Content Intent」 |
| `check_connections.py` のシートが失敗 | サービスアカウントへのシート共有（編集者）、`GOOGLE_SHEET_ID` |
| `verify_relay.py` が失敗 | `GAS_RELAY_URL` と `GAS_RELAY_TOKEN`（GAS のスクリプト プロパティと同じ値か） |
| `TypeError: 'function' object is not subscriptable`（`notes.py`） | コードの不具合（Python 3.12 などの古い Python でだけ起きる）で、修正済みです。パソコンで push し、VM で `cd ~/life-os-bot && git pull --ff-only` してから、もう一度実行します |
| `systemctl status` が `failed` / 終了コード 3 | `journalctl -u life-os-bot -n 30` を見る。「すでに別の場所で Bot が動いています」なら、パソコン側の Bot を停止する（クラッシュ直後なら約90秒待つ） |
| 何度も再起動を繰り返す | `journalctl -u life-os-bot -n 50` のエラーを確認 |
| Bot が反応しない | Discord のチャンネル名が `.env` の `CHANNEL_*` と一致しているか、ログにエラーが出ていないか |

## 安全のために

- `.env` と `credentials.json` は、VM の中だけに置きます（権限 600）。GitHub にもチャットにも貼らないでください。
- VM に外部から入れるのは、Google アカウントでログインした SSH だけです。Bot は外からの接続を待ち受けません（外向きの通信だけ）。
- デプロイキーは読み取り専用です。VM が万一乗っ取られても、GitHub のコードは書き換えられません。
- 秘密の値を変えたとき（トークンの再発行など）は、パソコンの `.env` を直して、手順 4 をやり直し、VM の Bot を再起動します。
