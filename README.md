# life-os-bot（人生管理OS）

Discord の 14 個のチャンネルに書き込むだけで、タスク・体調・日記・お金・アイデア・創作・レポートが記録される、自分専用の秘書 Bot です。
数字・日付・チェックは Google スプレッドシートへ、長い文章は Obsidian のノート（Google ドライブ上）へ保存します。
Python（discord.py）・Claude API・Google Sheets（gspread）・Google Drive API で作られています。

設計のトーンは「全肯定・責めない・黒衣（裏方）」です。未完了や記録の空白を責めず、頼まれていない助言はしません。

- **仕様は `SPEC.md` が正**です（チャンネルごとの動き・保存先・同期のルール・決定事項）。
- 今どこまで進んでいるか、次に何をするかは `docs/NEXT_STEPS.md` を見てください。
- コードを触る人（AI を含む）向けの注意は `CLAUDE.md` にあります。

## 14 個のチャンネル

| チャンネル | 役割 |
|---|---|
| `01-today-task` | 朝 8:00 のタスク案内、緊急タスクの追加、「完了 1,3」、昨日の執筆実績（10:00 以降） |
| `02-health` | Garmin・ヘルスケアのスクショと主観メモの記録、コンディション判定、「取扱マニュアル」 |
| `03-looking-back` | 夜 21:00 の問いかけ、日記の整形と保存、明日のタスクの選択 |
| `04-report` | 週次（日曜 20:00）・月次（月末日 20:00）レポート。投稿専用 |
| `05-private` | 私生活メモ・独り言（静かに記録。「行きたいカフェ教えて」のときだけ返信） |
| `06-household-accounts` | 家計簿（現金・娯楽）。今月の娯楽費の累計を返信 |
| `07-ledger` | 事業の帳簿（売上・経費）、レシート写真、転記チェック、今月の概算損益 |
| `08-scrap` | URL を投稿すると、要約・タグつきで保存 |
| `09-idea` | 思考の何でもゴミ箱（完全サイレント） |
| `10-project-novel` / `11-project-trpg` / `12-project-others` | 作品ごとのノート、進捗の記録、相談・検索 |
| `13-im-the-ceo` | 方針・宣言。全ての Claude への指示に、最優先で入る |
| `14-ai` | 総合秘書。シートとノートの検索・修正・削除（操作ログつき・取り消し可）、設定の変更 |

## 保存の考え方

- **数値・日付・フラグ → スプレッドシート**（シート名はチャンネル名と同じ）。
- **長い文章 → Obsidian**（`06-Life-OS/<チャンネル名>/` の中）。
- Obsidian は、**読み取りは Vault 全体で可、書き込みは `06-Life-OS/` の中と、タスク棚 1 ファイルだけ**です。
  この制限は `notes_policy.py` が強制し、全ての操作が `notes.GuardedStore` を通ります（詳しくは `SPEC.md` §2）。
- Google ドライブでは、サービスアカウントは新規ファイルを作れないため、新規作成・改名・ゴミ箱への移動は Google Apps Script の中継（`gas/`）が行います（`SPEC.md` §2.5）。

## セットアップ（開発用パソコン）

Python 3.14 の仮想環境 `.venv` を使います（本番の VM は Python 3.10〜3.12 なので、古い文法との互換をテストで守っています）。

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
```

1. `.env.example` を `.env` にコピーして、値を入れます（`DISCORD_TOKEN`、`ANTHROPIC_API_KEY`、`GOOGLE_SHEET_ID`、Drive のフォルダ ID、GAS 中継の URL とトークンなど）。
2. Google のサービスアカウントの鍵を `credentials.json` として置きます。
3. 接続を確認します: `.venv/Scripts/python scripts/check_connections.py`（秘密の値は表示しません）。GAS 中継の確認は `scripts/verify_relay.py`。
4. テスト: `.venv/Scripts/python -m pytest -q`
5. 起動: `.venv/Scripts/python main.py`

**秘密情報の扱い**: `.env` と `credentials.json`、GAS 中継のトークンは、GitHub に上げず、画面やログにも出しません（`.gitignore` で除外済み）。
認証はサービスアカウントだけで、OAuth は使いません。開発パソコンで証明書の検証に失敗する場合は、`.env` に `USE_TRUSTSTORE=1` を入れます（検証を無効にはしません。VM では設定しません）。

Bot は 1 か所でしか動かせません（パソコンと VM の両方で動くと、通知が重複するため）。2 つ目を起動しようとすると、断って終了します。パソコンで試すときは、先に VM の Bot を止めてください。

## 24 時間動かす（GCP の VM）

手順書は `docs/DEPLOY.md`、設定ファイルは `deploy/`（`life-os-bot.service`、`setup_vm.sh`、`update.sh`）です。
パソコンで直して GitHub に push したあと、VM で次を実行すると更新されます。

```bash
cd ~/life-os-bot && bash deploy/update.sh
```

VM 用の `.env` は `scripts/make_vm_env.py` で作ります。`.env` と `credentials.json` は、GitHub ではなく、VM に直接置きます。

## 定時ジョブ（日本時間）

| 時刻 | 内容 | 設定 |
|---|---|---|
| 08:00 | 朝のタスク案内（`01-today-task`） | `MORNING_TIME` |
| 10:00 以降 | 昨日の執筆実績（執筆記録シートに当日の行が入ってから 1 回） | `WRITING_TIME` |
| 20:00 | 日曜は週次レポート、月末日は月次レポート（`04-report`） | `REPORT_TIME` |
| 21:00 | 夜の問いかけ（`03-looking-back`） | `EVENING_TIME` |
| 10 分ごと | タスク棚（Obsidian）とシートの同期 | |

同じ日に二重投稿しないよう、実行済みの記録とチャンネル履歴の確認をしています。

## ファイルの案内

| 場所 | 内容 |
|---|---|
| `main.py` | Bot 本体。チャンネル名でハンドラに振り分ける |
| `handlers/` | チャンネルごとの処理（`today_task.py`、`health.py`、`ledger.py`、`ai.py` など） |
| `ai_tools.py` | `14-ai` が使うツール（シート・ノートの検索と変更、取り消し）と、その安全のための決まり |
| `report.py` / `scheduler.py` | 週次・月次レポートの組み立て / 定時ジョブ |
| `sheets.py` / `notes.py` / `notes_policy.py` | スプレッドシートの読み書き / Obsidian の読み書きとガード |
| `task_sync.py` / `task_dates.py` | タスク棚の同期 / 「明日」「期限は金曜まで」などの日付の解釈 |
| `writing_log.py` | 執筆記録シート（読み取り専用）の集計 |
| `claude_client.py` | Claude API の呼び出し。人格と、`13-im-the-ceo` の方針の注入 |
| `gas/` | Google Apps Script の中継（新規ファイルの作成など） |
| `scripts/` | 接続確認・中継の確認・レポートの試し作りなどの補助スクリプト |
| `tests/` | 自動テスト（本物のシートや Discord は使わない） |
| `docs/` | `DEPLOY.md`（VM の手順）、`NEXT_STEPS.md`（進み具合と次の作業） |
