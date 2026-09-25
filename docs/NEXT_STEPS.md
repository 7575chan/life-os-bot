# 今後の進め方（引き継ぎメモ）

最終更新: 2026-09-25。次に戻ってきたときは、このファイルを上から順に進める。

## 今の状態

- 実装・実機検証済み: 01-today-task / 02-life / 03-looking-back / 08-scrap / 09-idea / 10-project-novel / 11-project-trpg / 12-project-others、朝夕の定時投稿、タスク同期（Obsidian ⇄ シート）、多重起動の防止、GAS 中継、Drive の遅延・書き戻し対策。
- **実装済み・自動テストのみ（実機検証は未実施）**: 05-private（`handlers/private.py`。`tests/test_private.py`）。実機検証は、固有の目印つきの投稿で「保存・最新が上・タグ・シートの行・問い合わせへの返信」を確認し、終わったら `Inbox.md` とシートの行を元に戻す。
- 自動テスト: 477 件成功・1 件スキップ（Python 3.14）。
- **未実装**: 06-household-accounts / 07-ledger（フェーズ 2）、13-im-the-ceo / 14-ai（フェーズ 4）、04-report（フェーズ 5）。
- **VM には 08〜12 がまだ反映されていない**（下の手順 1）。

## 手順 1: VM に反映する（最初にやる）

1. パソコンで、変更をコミットして push する（コミットと push はユーザーが行う）。
   `git add -A` → `git commit -m "08-scrap と 10〜12 を追加"` → `git push`
   （`.env` `credentials.json` `data/` は `.gitignore` で除外済み。念のため `git status` で確認）
2. VM に SSH で入り、`bash deploy/update.sh` を実行する。
3. 手順は `docs/DEPLOY.md` を参照。パソコンの Bot が動いていないことを確認してから VM を起動する（多重起動の防止で、片方は終了コード 3 で止まる）。
4. Discord で確認する:
   - 08-scrap に記事の URL を投稿 → 📰 が付き、`06-Life-OS/08-scrap/` にノートができる。
   - 10-project-novel に「（作品名）2章まで書いた」と投稿 → 📝 が付き、`Novel_（作品名）.md` ができる。
   - 確認後、テストで作ったノートとシートの行は手で消してよい。
5. 問題があれば、VM で `journalctl -u life-os-bot -n 100` のログを見る。

## 手順 2: フェーズ 2（05 → 06 → 07）を実装する

Claude Code に「06-household-accounts を実装してください」のように、実装を明示して頼む。仕様は `SPEC.md` の §5（02_LIFE & MONEY）にある。

| 順 | チャンネル | 内容 | 注意点 |
|---|---|---|---|
| 1 | 05-private（実装済み・実機検証待ち） | 私生活の記録 | 手順 1 の VM 反映のあとに、実機で確認する |
| 2 | 06-household-accounts | 家計簿 | 金額・カテゴリの構造化 |
| 3 | 07-ledger | 事業の帳簿（短文メモ・レシート写真） | 写真は Vision で読み取る（新しい部分）。転記チェック2列、今月の概算損益の返信 |

進め方（これまでと同じ）:
1. 着手前に、SPEC の該当箇所と入力例を確認する。
2. 実装 → 自動テスト（偽の Drive・偽の AI を使う）→ 実機検証（固有の目印つきデータで試し、終了後に元へ戻す）。
3. `SPEC.md` §8.1・§10 と `CLAUDE.md` を更新する。
4. push して、VM で `bash deploy/update.sh`。

## 手順 3: 04-report（週次・月次レポート）

- 01〜12 のデータがある程度たまってから作る（数週間使ってから）。
- 出力先は `06-Life-OS/04-report/Weekly_*.md` `Monthly_*.md`（新規ファイルなので GAS 中継で作成する）。
- 仕様は `SPEC.md` の 04_CEO & SYSTEM の項。

## 手順 4: フェーズ 4（13-im-the-ceo / 14-ai）

- 13 の内容（`CEO-Directives.md`）は、すでに全ての Claude への指示の最優先として読み込まれている。実装するのは、ツールの実行と操作ログ。
- 設計の確認が必要なので、着手前にユーザーと決めること: どんなツールを実行させるか、操作ログの見せ方、実行の承認方法。
- `06-Life-OS/14-ai` フォルダは、多重起動の印（appProperties）に使っている。触るときは `instance_guard.py` に影響しないようにする。

## 仮定のまま進めている点（違っていたら伝える）

- X（Twitter）の投稿は、第三者サービス fxtwitter 経由で取得する。
- 作品名がない初回は `未命名_日付` で作り、後で改名する。
- 相談・検索の質問そのものは、ノートに追記しない。
- 1 投稿の URL は 3 件まで。危険な URL は保存せず拒否する。

## 再開するときの注意（Claude 向け）

- `CLAUDE.md` と `SPEC.md`（特に §5・§8.1・§10）を先に読む。
- 仕様書の作成・改訂を頼まれたときは、SPEC.md だけを編集し、実装は明示の指示があるまでしない。
- 開発用パソコンは Python 3.14、VM は 3.10〜3.12。`from __future__ import annotations` と `tests/test_py_compat.py` を維持する。
- 実機テストは、Sheets の読み取り上限（1 分 60 回）と VM の Bot の同期にぶつかる。固有の目印を付け、元に戻し、Bot のプロセスを残さない。
- Drive の遅延・同期ソフトの書き戻しは `CLAUDE.md` の「Drive quirks」を参照。新しい保存処理は、必ず `DriveStore` を通す。
- 実機テストのスクリプトは、バックスラッシュを含む文字列を bash のヒアドキュメントで渡さず、ファイルとして書く（`\n` が壊れる）。

## 未解決の小さな項目

- `09-idea/attachments/` の空フォルダが残っている（フォルダはゴミ箱に移せないため。害はない）。
- Python 3.12 では、`test_drive_store_relay.py` と `test_drive_recent.py` がこのパソコンの制限で実行できない（VM の 3.12 では実行できるはず）。
