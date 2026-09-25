"""`02-health`: 客観データ（スクショ）と主観メモの記録、コンディション判定、取扱マニュアル（SPEC §5）。"""
import asyncio
import logging
from datetime import timedelta

import claude_client
import health_analysis
import notes
import sheets
import util
import vault_paths

log = logging.getLogger("life-os.health")
MANUAL_KIND = "取扱マニュアル"  # 04-report シートの「種別」

_VISION = """これは Garmin や iPhone のヘルスケアなどのスクリーンショットです。読み取れる値だけを、JSONで返してください。
{"sleep_hours": 睡眠時間を時間の小数（例: 10時間15分 -> 10.25）か null,
 "deep_sleep_hours": 深い睡眠を時間の小数か null,
 "steps": 歩数（整数）か null,
 "stress": ストレススコア（整数）か null,
 "resting_heart_rate": 安静時心拍（整数）か null,
 "heart_rate": 心拍（整数）か null,
 "exercise": 運動の内容（短い文字列）か null}
画像に無い項目は null にする。推測しない。"""

_JUDGE = """あなたはユーザーの体調を静かに記録する秘書です。次の記録から、今のコンディションを判定してください。JSONだけを返します。
{{"judgement": "🟢 良好" か "🟡 ふつう" か "🔴 警戒" のどれか,
  "score": 体調スコア（1〜5の整数。5が最良）。判断できなければ null,
  "reply": ユーザーへの一言（40字以内・全肯定・助言や説教はしない。記録したことを伝えるだけでよい）}}

今回の記録:
{now}

直近の記録（参考）:
{recent}"""


def _hours_text(v) -> str:
    return "" if v in (None, "") else str(round(float(v), 2)).rstrip("0").rstrip(".")


def objective_text(obj: dict) -> str:
    bits = []
    if obj.get("stress") is not None:
        bits.append(f"ストレス {obj['stress']}")
    if obj.get("resting_heart_rate") is not None:
        bits.append(f"安静時心拍 {obj['resting_heart_rate']}")
    elif obj.get("heart_rate") is not None:
        bits.append(f"心拍 {obj['heart_rate']}")
    if obj.get("deep_sleep_hours") is not None:
        bits.append(f"深い睡眠 {_hours_text(obj['deep_sleep_hours'])}時間")
    return " / ".join(bits)


def steps_text(obj: dict) -> str:
    parts = []
    if obj.get("steps") is not None:
        parts.append(f"{int(obj['steps']):,}歩")
    if obj.get("exercise"):
        parts.append(str(obj["exercise"]))
    return " / ".join(parts)


async def handle(message) -> None:
    text = message.content.strip()
    images = await util.read_images(message)
    if not text and not images:
        return
    if "取扱マニュアル" in text:
        await _manual(message)
        return

    obj: dict = {}
    if images:
        got = await claude_client.vision_json(images, _VISION, {})
        obj = got if isinstance(got, dict) else {}
    row = {"日時": util.fmt_datetime(util.now()), "睡眠時間": _hours_text(obj.get("sleep_hours")),
           "歩数・運動": steps_text(obj), "客観指標": objective_text(obj), "主観メモ": text}

    recent = await asyncio.to_thread(sheets.records, sheets.HEALTH)
    recent_txt = "\n".join(f"{r['日時']} 睡眠{r['睡眠時間']}h {r['客観指標']} メモ:{r['主観メモ']} → {r['AI判定']}"
                           for _, r in recent[-7:]) or "（まだありません）"
    now_txt = "\n".join(f"{k}: {v}" for k, v in row.items() if v and k != "日時") or "（記録のみ）"
    judge = await claude_client.complete_json(_JUDGE.format(now=now_txt, recent=recent_txt),
                                              {"judgement": "🟡 ふつう", "score": None, "reply": "記録しました。"})
    if not isinstance(judge, dict):
        judge = {"judgement": "🟡 ふつう", "score": None, "reply": "記録しました。"}
    row["AI判定"] = str(judge.get("judgement") or "")
    score = util.clamp_int(judge.get("score"), 1, 5)
    row["体調スコア"] = score or ""
    await asyncio.to_thread(sheets.append, sheets.HEALTH, row)
    await util.ack(message, "🩺")
    summary = [f"{row['AI判定']}" + (f"（スコア {score}）" if score else "")]
    if row["睡眠時間"]:
        summary.append(f"睡眠 {row['睡眠時間']}時間")
    if row["歩数・運動"]:
        summary.append(row["歩数・運動"])
    reply = str(judge.get("reply") or "記録しました。")
    await message.channel.send(" ・ ".join(summary) + "\n" + reply)


_MANUAL_PROMPT = """次のデータから、ユーザーの「自分取扱マニュアル」を作ってください。

ルール:
- データから言えることだけを書く。データが少ないときは「まだ傾向は見えていません」と正直に書く。
- 評価・説教・べき論は書かない。ユーザーが自分を扱いやすくするための、事実と対処のメモにする。
- 箇条書き。3つの見出し: 「睡眠と気分の法則」「警戒サイン」「うまくいく過ごし方」。
- 数字は与えられた集計をそのまま使う。作らない。

睡眠と気分の集計（しきい値 {th} 時間）:
{table}

最近の体調の記録:
{rows}"""


async def update_manual(today) -> str:
    """「自分取扱マニュアル」を作り直して `Health-Manual.md` に保存し、`04-report` シートに記録する。本文を返す。

    `02-health` で「取扱マニュアル」と書いたときと、週次・月次レポート（`report.py`）から呼ばれる。
    手で書いた内容を失わないよう、上書きの前に、それまでの内容を `Health-Manual-前回.md` に残す（残せなければ上書きしない）。
    AI が空の文章を返したときも、上書きしない。"""
    health = await asyncio.to_thread(sheets.between, sheets.HEALTH, today - timedelta(days=90), today + timedelta(days=1))
    diary = await asyncio.to_thread(sheets.between, sheets.DIARY, today - timedelta(days=90), today + timedelta(days=1))
    table = health_analysis.sleep_mood_table([r for _, r in health], [r for _, r in diary])
    rows = "\n".join(f"{r['日時']} 睡眠{r['睡眠時間']}h {r['客観指標']} メモ:{r['主観メモ']} → {r['AI判定']}"
                     for _, r in health[-40:]) or "（記録がまだありません）"
    body = (await claude_client.complete(_MANUAL_PROMPT.format(th=table["threshold_hours"], table=table, rows=rows),
                                         max_tokens=1500) or "").strip()
    if not body:
        raise ValueError("マニュアルの文章を作れませんでした")
    doc = f"# 自分取扱マニュアル\n\n更新: {util.fmt_datetime(util.now())}\n\n{body}\n"
    store = notes.get_store()
    old = await asyncio.to_thread(store.read, vault_paths.health_manual())
    if old and old.strip():
        await asyncio.to_thread(store.write, vault_paths.health_manual_previous(), old)  # 前の版を残す（失敗したら、ここで止まる）
    await asyncio.to_thread(store.write, vault_paths.health_manual(), doc)  # Obsidian を先に
    try:
        await asyncio.to_thread(sheets.append, sheets.REPORTS, {"発行日": util.fmt_date(today), "種別": MANUAL_KIND, "本文": doc[:30_000]})
    except Exception:  # noqa: BLE001  ノートには保存済み
        log.warning("取扱マニュアルを 04-report シートに記録できませんでした", exc_info=True)
    return body


async def _manual(message) -> None:
    body = await update_manual(util.today())
    await util.ack(message, "📖")
    await util.send_long(message.channel, "自分取扱マニュアルを更新しました📖\n\n" + body)
