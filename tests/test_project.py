import asyncio
from datetime import date, datetime
from types import SimpleNamespace as NS

import pytest

import claude_client
import config
import notes
import sheets
import state
import task_sync
import util
import vault_paths
import writing_log as wl
from handlers import project

NOW = datetime(2026, 9, 20, 10, 30, tzinfo=config.TZ)
TODAY = date(2026, 9, 20)
ROOMS = [("novel", "Novel", "10-project-novel"), ("trpg", "TRPG", "11-project-trpg"), ("others", "Project", "12-project-others")]


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 純粋関数


def test_clean_name_and_key():
    assert project.clean_name("『月の庭』") == "月の庭"
    assert project.clean_name(' 「a/b:c*?」 ') == "abc"
    assert project.clean_name(None) == "" and project.clean_name(5) == ""
    assert len(project.clean_name("あ" * 100)) == project.MAX_NAME
    assert project.norm_key("月の 庭") == project.norm_key("月の庭") and project.norm_key("ＡＢＣ") == "abc"


def test_list_works_top_level_prefix_only():
    d = "06-Life-OS/10-project-novel"
    paths = [f"{d}/Novel_月の庭.md", f"{d}/Novel_未命名_20260920.md", f"{d}/Other_x.md", f"{d}/sub/Novel_深い.md",
             "06-Life-OS/11-project-trpg/Novel_別の部屋.md", f"{d}/Novel_.md", f"{d}/Novel_a.txt"]
    assert project.list_works("Novel", paths, d) == sorted(["未命名_20260920", "月の庭"])


@pytest.mark.parametrize("ai,new,existing,last,expected", [
    ("月の庭", False, ["月の庭", "海"], None, "月の庭"),               # 既存の作品
    ("月 の庭", False, ["月の庭"], None, "月の庭"),                     # 空白の違いは同じ作品
    ("新作", True, ["月の庭"], "月の庭", "新作"),                        # 新しい作品
    (None, False, ["月の庭", "海"], "海", "海"),                         # 分からなければ直前の作品
    (None, False, ["月の庭"], None, "月の庭"),                           # 作品が1つだけなら、それ
    ("知らない名前", False, ["月の庭", "海"], "海", "海"),               # 既存に無く「新規」でもない → 直前の作品
    ("知らない名前", False, ["月の庭", "海"], None, "知らない名前"),
    (None, False, [], None, "未命名_20260920"),                          # 何も無ければ未命名
    ("『新作』", True, [], None, "新作"),
])
def test_resolve_work(ai, new, existing, last, expected):
    assert project.resolve_work(ai, new, existing, last, TODAY) == expected


def test_normalize_kind():
    assert project.normalize_kind("進捗", None) == "進捗"
    assert project.normalize_kind("知らない種別", None) == "その他" and project.normalize_kind(None, None) == "その他"
    assert project.normalize_kind("進捗", "月の庭") == "タイトル決定"


def test_format_entry_and_task_text():
    files = [{"name": "map.png", "saved": True, "image": True, "reason": ""}]
    assert project.format_entry(NOW, "進捗", "2章まで書いた\n## 偽見出し", files) == \
        "## 2026-09-20 10:30｜進捗\n2章まで書いた\n\\## 偽見出し\n![[map.png]]"
    assert project.task_text("月の庭", "会話イベントを書く") == "月の庭｜会話イベントを書く"


def test_consult_prompt_contains_all_the_materials():
    p = project.consult_prompt(label="小説", kind="相談", text="今月中に初稿を終えたい", work="月の庭", others=["海"],
                               note="# 月の庭\n\n黒幕は姉", progress="総文字数 3,000", snippets=[{"path": "a/b/メモ.md", "snippet": "伏線"}])
    for part in ("今月中に初稿を終えたい", "月の庭", "海", "黒幕は姉", "総文字数 3,000", "- メモ.md: 伏線"):
        assert part in p
    assert "ノートはまだありません" in project.consult_prompt(label="x", kind="検索", text="t", work="w", others=[], note=None, progress=None, snippets=[])
    assert len(project.tail("あ" * 50_000)) < project.NOTE_CONTEXT_LIMIT + 50


# ---------------------------------------------------------------- 執筆記録シートとの照合・進捗

D0 = 46265.4  # 2026-08-31


def wlog(rows):
    header = ["a", "港町の人魚", "2025-05 K課の話", "2026-09"]
    return wl.parse([header, ["目標", 30000, 20000, 50000], ["締切", 46295, 46203, 46295], ["単位", 1, 1, 1], ["id", "x", "y", "z"], *rows])


def test_match_work_is_conservative():
    log = wlog([[D0, 100, 200, 300]])
    assert wl.match_work("港町の人魚", log.works).name == "港町の人魚"
    assert wl.match_work("港町", log.works).name == "港町の人魚"          # 一方が他方を含む（1件だけ）
    assert wl.match_work("K課の話", log.works).name == "K課の話"           # 「YYYY-MM 」の接頭辞は無視
    assert wl.match_work("未命名_20260920", log.works) is None            # 曖昧・無関係は紐付けない
    assert wl.match_work("", log.works) is None
    assert wl.match_work("の", log.works) is None                          # 1文字では部分一致にしない
    assert wl.match_work("2026-09-20 メモ", log.works) is None            # 数字だけの作品名「2026-09」に、数字の部分一致で紐付けない
    assert wl.match_work("未命名_202609", log.works) is None               # 名前が決まっていない作品は照合しない
    assert wl.match_work("2026-09", log.works).name == "2026-09"          # 完全一致なら数字だけの名前でも紐付く


def test_progress_summary_numbers():
    rows = [[D0 + i, 1000 * i, 0, 0] for i in range(0, 10)]  # 港町: 毎日+1000、最新は 9,000
    p = wl.progress_summary(wlog(rows), "港町の人魚", date(2026, 9, 9))
    assert p["total"] == 9000 and p["target"] == 30000 and p["pct"] == 30
    assert p["added_7d"] == 7000 and p["avg_per_day"] == 1000
    assert p["days_left"] == 21 and p["remaining"] == 21000 and p["need_per_day"] == 1000
    text = wl.format_progress(p)
    assert "総文字数 9,000 / 目標 30,000（30%）" in text and "締切 2026-09-30（あと21日）" in text and "1,000文字/日" in text
    assert wl.progress_summary(wlog(rows), "無関係な作品", TODAY) is None
    assert wl.progress_summary(wlog([]), "港町の人魚", TODAY) is None


# ---------------------------------------------------------------- 処理（偽の AI・シート、実際のファイル）


@pytest.fixture(params=ROOMS, ids=[r[0] for r in ROOMS])
def env(request, tmp_path, monkeypatch):
    key, prefix, sheet = request.param
    room = f"06-Life-OS/{sheet}"
    (tmp_path / room).mkdir(parents=True)
    (tmp_path / "06-Life-OS" / "08-scrap").mkdir(parents=True)
    store = notes.GuardedStore(notes.LocalStore(tmp_path))
    kv = {}
    e = NS(key=key, prefix=prefix, sheet=sheet, room=room, root=tmp_path, store=store, rows=[], tasks=[], syncs=[], sent=[],
           prompts=[], kv=kv, ai={"work": None, "is_new_work": False, "kind": "進捗", "title_decided": None, "task": None,
                                  "search_query": None},
           answer="相談への答え", writing=None, sheet_fail=False)

    async def fake_ai(prompt, fallback, **kw):
        e.prompts.append(prompt)
        return e.ai if e.ai is not None else fallback

    async def fake_complete(prompt, **kw):
        e.prompts.append(prompt)
        e.system = kw.get("extra_system")
        return e.answer

    def fake_append(name, data):
        if e.sheet_fail:
            raise RuntimeError("sheet down")
        e.rows.append((name, data))
        return 2

    monkeypatch.setattr(notes, "get_store", lambda: store)
    monkeypatch.setattr(claude_client, "complete_json", fake_ai)
    monkeypatch.setattr(claude_client, "complete", fake_complete)
    monkeypatch.setattr(sheets, "append", fake_append)
    monkeypatch.setattr(sheets, "add_task", lambda content, **kw: e.tasks.append((content, kw)) or "lo-x")
    monkeypatch.setattr(task_sync, "run_safely", lambda: e.syncs.append(1))
    monkeypatch.setattr(state, "get_kv", lambda k, d=None: kv.get(k, d))
    monkeypatch.setattr(state, "set_kv", lambda k, v: kv.__setitem__(k, v))
    monkeypatch.setattr(util, "now", lambda: NOW)
    monkeypatch.setattr(util, "today", lambda: TODAY)
    monkeypatch.setattr(wl, "fetch", lambda: e.writing)
    monkeypatch.setattr(project, "_locks", {})

    class Att:
        def __init__(self, filename, content_type, data=b"x"):
            self.filename, self.content_type, self.data, self.size = filename, content_type, data, len(data)

        async def read(self):
            return self.data

    class Msg:
        def __init__(self, content="", attachments=None):
            self.content, self.attachments, self.reactions = content, attachments or [], []
            self.channel = NS(send=self._send)

        async def _send(self, text, reference=None):
            e.sent.append(text)

        async def add_reaction(self, emoji):
            self.reactions.append(emoji)

    e.Msg, e.Att = Msg, Att
    e.handler = project.make_handler(key)
    e.note = lambda name: (tmp_path / room / f"{prefix}_{name}.md")
    e.notes = lambda: sorted(p.name for p in (tmp_path / room).glob("*.md"))
    return e


def post(env, text, **kw):
    m = env.Msg(text, **kw)
    run(env.handler(m))
    return m


def test_first_post_creates_an_untitled_work_silently(env):
    m = post(env, "1章のプロットを書いた")
    assert env.notes() == [f"{env.prefix}_未命名_20260920.md"]
    text = env.note("未命名_20260920").read_text(encoding="utf-8")
    assert text.startswith("# 未命名_20260920\n\n## 2026-09-20 10:30｜進捗\n1章のプロットを書いた")
    assert env.sent == [] and m.reactions == ["📝"]  # 相談以外は返信しない
    assert env.rows == [(env.sheet, {"日時": "2026-09-20 10:30", "作品名": "未命名_20260920", "種別": "進捗", "内容": "1章のプロットを書いた"})]
    assert env.kv[f"project_last:{env.key}"] == "未命名_20260920"


def test_next_posts_go_to_the_last_touched_work_and_stay_in_order(env):
    post(env, "一つ目")
    post(env, "二つ目")
    text = env.note("未命名_20260920").read_text(encoding="utf-8")
    assert env.notes() == [f"{env.prefix}_未命名_20260920.md"] and text.index("一つ目") < text.index("二つ目")  # 作品ノートは時系列（下に追記）


def test_ai_can_route_to_an_existing_work_or_start_a_new_one(env):
    env.ai = {**env.ai, "work": "月の庭", "is_new_work": True}
    post(env, "新しい作品を始める")
    env.ai = {**env.ai, "work": "海", "is_new_work": True}
    post(env, "別の作品も始める")
    env.ai = {**env.ai, "work": "月の 庭", "is_new_work": False}  # 空白違いでも既存
    post(env, "月の庭の続き")
    assert env.notes() == [f"{env.prefix}_月の庭.md", f"{env.prefix}_海.md"]
    assert "月の庭の続き" in env.note("月の庭").read_text(encoding="utf-8")
    assert env.kv[f"project_last:{env.key}"] == "月の庭"
    assert "既存の作品: 月の庭、海" in env.prompts[-1] or "既存の作品: 海、月の庭" in env.prompts[-1]


def test_next_todo_becomes_a_high_priority_task_and_later_becomes_backlog(env):
    env.ai = {**env.ai, "work": "月の庭", "is_new_work": True, "kind": "次やる", "task": "会話イベントを書く 明日まで"}
    m = post(env, "次は会話イベントを書く。明日まで")
    assert env.tasks == [("月の庭｜会話イベントを書く", {"scheduled": "", "due": "2026-09-21", "priority": "高", "source": env.sheet})]
    assert env.syncs == [1] and m.reactions == ["📝", "📋"] and env.sent == []
    env.ai = {**env.ai, "work": None, "is_new_work": False, "kind": "後回し", "task": "伏線の回収"}
    post(env, "伏線の回収は後回し")
    assert env.tasks[-1] == ("月の庭｜伏線の回収", {"scheduled": "", "due": "", "priority": "", "source": env.sheet})  # バックログ


def test_title_decided_renames_the_file_and_the_heading(env):
    post(env, "始まりの記録")
    env.ai = {**env.ai, "title_decided": "月の庭", "kind": "その他"}
    m = post(env, "タイトルは『月の庭』に決まった")
    assert env.notes() == [f"{env.prefix}_月の庭.md"]  # 未命名のファイルは無くなり、改名される
    text = env.note("月の庭").read_text(encoding="utf-8")
    assert text.startswith("# 月の庭\n") and "始まりの記録" in text and "｜タイトル決定" in text
    assert "🏷️" in m.reactions and env.sent == ["🏷️ 『未命名_20260920』を『月の庭』に改名しました。"]
    assert env.kv[f"project_last:{env.key}"] == "月の庭"
    assert env.rows[-1][1]["作品名"] == "月の庭" and env.rows[-1][1]["種別"] == "タイトル決定"


def test_rename_to_an_existing_title_is_refused_and_changes_nothing(env):
    env.ai = {**env.ai, "work": "A", "is_new_work": True}
    post(env, "Aを始める")
    env.ai = {**env.ai, "work": "B", "is_new_work": True}
    post(env, "Bを始める")
    before = {n: (env.root / env.room / n).read_text(encoding="utf-8") for n in env.notes()}
    env.ai = {**env.ai, "work": "A", "is_new_work": False, "title_decided": "B", "kind": "その他"}
    env.sent.clear()
    post(env, "タイトルはBに決まった")
    assert env.sent == ["改名できませんでした: 『B』という作品がすでにあります。"]
    assert {n: (env.root / env.room / n).read_text(encoding="utf-8") for n in env.notes()} == before


def test_title_decided_with_no_works_starts_the_work_under_that_title(env):
    env.ai = {**env.ai, "title_decided": "最初から決まっている題", "kind": "その他"}
    post(env, "タイトルは『最初から決まっている題』にする")
    assert env.notes() == [f"{env.prefix}_最初から決まっている題.md"] and env.sent == []


def test_consult_replies_using_note_progress_and_search_and_does_not_pollute_the_note(env):
    env.ai = {**env.ai, "work": "港町の人魚", "is_new_work": True, "kind": "進捗"}
    post(env, "黒幕は姉に決めた")
    (env.root / "06-Life-OS" / "08-scrap" / "2026-09-01_参考記事.md").write_text("伏線の張り方について", encoding="utf-8")
    env.writing = wlog([[D0 + i, 1000 * i, 0, 0] for i in range(10)])
    before = env.note("港町の人魚").read_text(encoding="utf-8")
    env.ai = {**env.ai, "work": "港町の人魚", "is_new_work": False, "kind": "相談", "search_query": "伏線"}
    m = post(env, "今月中に初稿を終えたいんだけど、どう分ければいい？")
    assert env.sent == ["相談への答え"] and m.reactions == ["💬"]
    prompt = env.prompts[-1]
    assert "黒幕は姉に決めた" in prompt                      # 作品のノート
    assert "総文字数 9,000 / 目標 30,000" in prompt          # 執筆記録シートの数字
    assert "参考記事.md: " in prompt and "伏線" in prompt    # スクラップの検索結果
    assert "評価・説教・べき論は書かない" in env.system and "具体的で実行しやすい提案" in env.system
    assert env.note("港町の人魚").read_text(encoding="utf-8") == before  # 相談の質問はノートに追記しない
    assert env.rows[-1][1]["種別"] == "相談"                 # シートには記録する


def test_search_kind_replies_and_records(env):
    env.ai = {**env.ai, "kind": "検索", "search_query": "灯台"}
    (env.root / env.room / f"{env.prefix}_海.md").write_text("# 海\n\n灯台守は幽霊", encoding="utf-8")
    m = post(env, "前に言ってた灯台の設定って何だっけ")
    assert env.sent == ["相談への答え"] and "灯台守は幽霊" in env.prompts[-1] and m.reactions == ["💬"]
    assert env.notes() == [f"{env.prefix}_海.md"]  # 検索だけでは、新しいノートを作らない


def test_ai_failure_still_records_under_the_last_work(env):
    post(env, "最初の投稿")
    env.ai = None  # complete_json のフォールバック（種別=その他、作品=不明）
    m = post(env, "AI が使えなくても記録する")
    assert "AI が使えなくても記録する" in env.note("未命名_20260920").read_text(encoding="utf-8")
    assert m.reactions == ["📝"] and env.sent == [] and env.rows[-1][1]["種別"] == "その他"


def test_images_are_saved_in_the_rooms_attachments_folder(env):
    m = post(env, "ダンジョンのマップ", attachments=[env.Att("map.png", "image/png", b"\x89PNG-map")])
    saved = list((env.root / env.room / "attachments").glob("*.png"))
    assert len(saved) == 1 and saved[0].read_bytes() == b"\x89PNG-map"
    assert f"![[{saved[0].name}]]" in env.note("未命名_20260920").read_text(encoding="utf-8") and m.reactions == ["📝"]


def test_obsidian_failure_records_nothing_else(env, monkeypatch):
    monkeypatch.setattr(env.store, "append_entry", lambda *a, **k: (_ for _ in ()).throw(OSError("drive down")))
    env.ai = {**env.ai, "kind": "次やる", "task": "やること"}
    m = env.Msg("保存できない")
    with pytest.raises(OSError):
        run(env.handler(m))
    assert env.rows == [] and env.tasks == [] and m.reactions == [] and env.kv == {}


def test_sheet_failure_keeps_the_note_and_warns(env):
    env.sheet_fail = True
    m = post(env, "シートは失敗")
    assert "シートは失敗" in env.note("未命名_20260920").read_text(encoding="utf-8") and m.reactions == ["📝", "⚠️"] and env.sent == []


def test_empty_message_is_ignored(env):
    m = post(env, "  ")
    assert env.notes() == [] and env.rows == [] and m.reactions == []


def test_concurrent_posts_are_not_lost(env):
    async def many():
        await asyncio.gather(*(env.handler(env.Msg(f"同時の投稿{i}")) for i in range(6)))

    run(many())
    text = env.note("未命名_20260920").read_text(encoding="utf-8")
    assert all(f"同時の投稿{i}" in text for i in range(6)) and text.count("\n## ") == 6


def test_writes_stay_inside_the_room_folder_and_each_room_is_independent(env):
    post(env, "書き込み先の確認", attachments=[env.Att("a.png", "image/png")])
    files = {p.relative_to(env.root).as_posix() for p in env.root.rglob("*") if p.is_file()}
    assert all(f.startswith(env.room + "/") for f in files), files
    assert env.rows[0][0] == env.sheet  # 部屋ごとの別シート
