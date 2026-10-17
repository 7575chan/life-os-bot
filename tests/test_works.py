"""作品管理シート（人生のすべてのプロジェクト。SPEC §5 03_CREATIVE「作品管理シート」）。"""
import asyncio
from datetime import date
from types import SimpleNamespace as NS

import pytest

import claude_client
import config
import notes
import sheets
import stages
import state
import task_sync
import board
import util
import works
from handlers import project, today_task, works_cmd

DAY = date(2026, 10, 16)  # 金曜
HEADER = ["#", "今やる？", "プロジェクト", "タイトル", "状況", "完了度", "現在の文字数", "残り文字数", "目標文字数", "工程",
          "ストップ理由", "URL", "シート", "優先順位", "重要度", "収益化", "最終更新日", "次やること", "締切"]


def row(no, now, kind, title, status="素材あり", stage="", chars="", target="", stop="", sheet_url="", next_="", deadline=""):
    return [str(no), now, kind, title, status, "", chars, "", target, stage, stop, "", sheet_url, "中", "", "低", "2026/09/06",
            next_, deadline]


VALUES = [HEADER,
          row(4, "YES", "TRPG", "M.M.5s", stage="校正01", chars="35305", target="40000"),
          row(13, "-", "TRPG", "『悪夢のなかから連れ出して』"),
          row(30, "YES", "小説（長編）", "ひよのの乙女ゲームパロ第一部＆第二部（20万字）", status="製作中"),
          row(46, "YES", "私生活", "運動習慣をつける", status="保留"),
          row(47, "-", "小説（短編）", "記憶喪失ジュードのジュ駒"),
          row(48, "-", "小説（短編）", "リン駒のとりかへばや物語"),
          ["", "", "", "", ""]]


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 読み取り（純粋）


def test_parse_reads_by_header_name_and_skips_blank_titles():
    header, ps = works.parse(VALUES)
    assert header[9] == "工程" and len(ps) == 6
    p = ps[0]
    assert (p["row"], p["no"], p["now"], p["kind"], p["stage"], p["chars"], p["target"]) == (2, 4, True, "TRPG", "校正01", 35305, 40000)
    shuffled = [list(reversed(r)) for r in VALUES]  # 列の並びが変わっても読める
    assert works.parse(shuffled)[1][0]["title"] == "M.M.5s"


@pytest.mark.parametrize("q, titles", [
    ("4", ["M.M.5s"]), ("4番", ["M.M.5s"]), ("#4", ["M.M.5s"]), ("＃４", ["M.M.5s"]),
    ("悪夢", ["『悪夢のなかから連れ出して』"]), ("悪夢のなかから連れ出して", ["『悪夢のなかから連れ出して』"]),
    ("駒", ["記憶喪失ジュードのジュ駒", "リン駒のとりかへばや物語"]), ("99番", []), ("ない作品", []),
])
def test_find(q, titles):
    assert [p["title"] for p in works.find(q, works.parse(VALUES)[1])] == titles


def test_split_target_separates_the_stage():
    ps = works.parse(VALUES)[1]
    cands, extra = works_cmd.split_target("4番 校正02", ps)
    assert [p["no"] for p in cands] == [4] and extra == "校正02"
    cands, extra = works_cmd.split_target("M.M.5s 本文改稿01", ps)
    assert [p["no"] for p in cands] == [4] and extra == "本文改稿01"
    assert works_cmd.split_target("悪夢", ps)[1] == ""


def test_stage_templates():
    t = stages.parse(stages.DEFAULT + "\n## 作品: M.M.5s\n- 校正01\n- 出品\n")
    assert stages.steps_for(t, "小説（短編）", "x")[0] == "簡易プロット作成"
    assert stages.steps_for(t, "TRPG", "x")[0] == "文字起こし修正"
    assert stages.steps_for(t, "TRPG", "M.M.5s") == ["校正01", "出品"]  # 作品ごとの直し
    assert stages.is_continuing(stages.steps_for(t, "私生活", "運動"))
    assert stages.steps_for(t, "謎の種類", "x") is None
    steps = stages.steps_for(t, "小説", "x")
    assert stages.next_step(steps, "") == "簡易プロット作成"
    assert stages.next_step(steps, "校正01") == "本文改稿01"
    assert stages.next_step(steps, "出品・公開") is None and stages.next_step(steps, "自由な段階") is None
    assert "▶ 4. 校正01" in stages.show(steps, "校正01")


def test_parse_outline_lists_unfinished_scenes():
    values = [[""], ["", "＃", "タイトル"], ["", "見出し1", "見出し2", "見出し3", "完了"],
              ["", "◆一日目", "", "", "TRUE"], ["", "", "○午前", "", "FALSE"], ["", "", "", "・海の家", "FALSE"],
              ["", "", "", "", ""]]
    assert works.parse_outline(values) == ["○午前", "・海の家"]
    assert works.parse_outline([["a"], ["b"]]) == []


def test_week_count_and_morning_section():
    ps = works.parse(VALUES)[1]
    run_p = next(p for p in ps if p["no"] == 46)
    tasks = [{"done": True, "source": "作品#46", "content": "運動習慣をつける｜やった", "done_date": "2026-10-12"},
             {"done": True, "source": "作品#46", "content": "運動習慣をつける｜やった", "done_date": "2026-10-16"},
             {"done": True, "source": "作品#46", "content": "運動習慣をつける｜やった", "done_date": "2026-10-11"},  # 先週
             {"done": False, "source": "作品#46", "content": "運動習慣をつける｜ジョギング", "done_date": ""},
             {"done": True, "source": "作品#46", "content": "別の作品｜やった", "done_date": "2026-10-16"}]
    assert works.week_count(run_p, tasks, DAY) == 2
    text = works.morning_section(ps, tasks, DAY)
    assert text.startswith("📂 今やるプロジェクト：")
    assert "・M.M.5s（校正01）" in text and "・運動習慣をつける（今週 2回）" in text and "悪夢" not in text
    assert works.morning_section([p for p in ps if not p["now"]], tasks, DAY) is None


def test_morning_post_puts_projects_before_tasks():
    text = today_task.compose_morning([{"content": "企画書", "priority": "高", "due": "", "row": 2}], None, [],
                                      "📂 今やるプロジェクト：\n・M.M.5s（校正01）")
    assert text.index("今やるプロジェクト") < text.index("本日のメインタスク")


# ---------------------------------------------------------------- 書き込み（決まった4列だけ）


class FakeWS:
    def __init__(self, values):
        self.values = [list(r) for r in values]
        self.updates = []

    def get_all_values(self):
        return [list(r) for r in self.values]

    def row_values(self, r):
        return list(self.values[r - 1])

    def batch_update(self, data, value_input_option="RAW"):
        from gspread.utils import a1_to_rowcol
        for d in data:
            r, c = a1_to_rowcol(d["range"])
            self.values[r - 1][c - 1] = d["values"][0][0]
            self.updates.append((d["range"], d["values"][0][0], value_input_option))


@pytest.fixture
def ws(monkeypatch):
    w = FakeWS(VALUES)
    logs = []
    monkeypatch.setattr(config, "WORKS_SHEET_ID", "book-id")
    monkeypatch.setattr(works, "_worksheet", lambda: w)
    monkeypatch.setattr(sheets, "log_operation", lambda *a: logs.append(a) or 2)
    monkeypatch.setattr(util, "activity_day", lambda at=None: DAY)
    works.clear_cache()
    w.logs = logs
    yield w
    works.clear_cache()


def test_write_only_allowed_columns_and_logs_first(ws):
    p = works.load()[1]  # #13
    works.write(p, "テスト", **{works.COL_NOW: "YES", works.COL_UPDATED: "2026/10/16"})
    assert ws.values[2][1] == "YES" and ws.values[2][16] == "2026/10/16"
    assert [u[2] for u in ws.updates] == ["RAW", "USER_ENTERED"]  # 日付は日付として入れる
    assert ws.logs[0][0] == "テスト" and ws.logs[0][2] == 3 and ws.logs[0][3] == {"今やる？": "-", "最終更新日": "2026/09/06"}
    assert works.load()[1]["now"] is True  # 覚えている一覧も更新
    with pytest.raises(ValueError):
        works.write(p, "テスト", **{"完了度": "x"})
    with pytest.raises(ValueError):
        works.write(p, "テスト", **{"タイトル": "x"})


def test_write_refuses_when_the_row_changed(ws):
    p = works.load()[1]
    ws.values[2][3] = "並べ替えで来た別の作品"
    with pytest.raises(works.WorksError):
        works.write(p, "テスト", **{works.COL_STAGE: "校正01"})
    assert ws.updates == [] and ws.logs == []


def test_write_reports_a_missing_column(ws):
    ws.values[0][9] = "NA"  # 見出しを「工程」に変えていない
    p = works.load(refresh=True)[0]
    with pytest.raises(works.WorksError, match="工程"):
        works.write(p, "テスト", **{works.COL_STAGE: "校正02"})


def test_touch_tasks_only_for_project_tasks(ws):
    works.touch_tasks([{"source": "作品#4", "content": "M.M.5s｜導入を推敲"},
                       {"source": "作品#13", "content": "別の作品｜x"},  # タイトルが合わない → 書かない
                       {"source": "01-today-task", "content": "牛乳"}])
    assert [u[0] for u in ws.updates] == ["Q2"]


def test_sync_touches_projects_completed_in_obsidian(monkeypatch):
    seen = []
    monkeypatch.setattr(works, "touch_tasks", lambda ts: seen.extend(t["id"] for t in ts))
    before = [{"id": "a", "done": False, "source": "作品#4"}, {"id": "b", "done": True, "source": "作品#4"},
              {"id": "c", "done": False, "source": "作品#4"}]
    task_sync._touch_projects(before, [("a", {"done": True}), ("b", {"done": True}), ("c", {"content": "x"})])
    assert seen == ["a"]


# ---------------------------------------------------------------- コマンド（プロジェクトの部屋）


@pytest.fixture
def env(ws, monkeypatch, tmp_path):
    (tmp_path / "06-Life-OS").mkdir()
    store = notes.GuardedStore(notes.LocalStore(tmp_path))
    monkeypatch.setattr(notes, "get_store", lambda: store)
    e = NS(sent=[], reactions=[], added=[], pending={}, ws=ws, store=store, tasks=[])

    async def send(text, reference=None):
        e.sent.append(text)
        return NS(id=100 + len(e.sent))

    async def react(emoji):
        e.reactions.append(emoji)

    def add_task(content, **kw):
        e.added.append((content, kw))
        e.tasks.append({"content": content, "source": kw.get("source"), "done": kw.get("done", False),
                        "done_date": kw.get("done_date", "")})
        return "lo-x"

    monkeypatch.setattr(sheets, "add_task", add_task)
    monkeypatch.setattr(sheets, "all_tasks", lambda: list(e.tasks))
    monkeypatch.setattr(task_sync, "run_safely", lambda: None)
    e.refreshed = 0

    async def refresh(force=False):
        e.refreshed += 1
        return None

    monkeypatch.setattr(board, "refresh", refresh)
    monkeypatch.setattr(state, "put_pending", lambda mid, cid, kind, payload: e.pending.update(
        {mid: {"message_id": mid, "channel_id": cid, "kind": kind, "payload": payload}}))
    monkeypatch.setattr(state, "get_pending", lambda mid: e.pending.get(mid))
    monkeypatch.setattr(state, "delete_pending", lambda mid: e.pending.pop(mid, None))
    monkeypatch.setattr(state, "latest_pending", lambda cid, kind, max_age_hours=12: (
        list(e.pending.values())[-1] if e.pending else None))
    e.channel = NS(id=7, send=send)
    e.msg = lambda text, ref=None: NS(content=text, channel=e.channel, add_reaction=react, attachments=[],
                                      reference=NS(message_id=ref) if ref else None)
    return e


def cmd(e, text, ref=None):
    return run(works_cmd.handle(e.msg(text, ref), text))


def test_disabled_without_sheet_id(env, monkeypatch):
    monkeypatch.setattr(config, "WORKS_SHEET_ID", "")
    assert cmd(env, "今やる") is False


def test_not_a_command(env):
    assert cmd(env, "今日は導入を3000字書いた") is False


def test_list_now_projects_and_note_over_three(env):
    assert cmd(env, "今やる")
    text = env.sent[-1]
    assert text.startswith("📂 今やるプロジェクト：") and "#4 M.M.5s" in text and "工程: 校正01" in text
    assert "35,305／40,000字" in text and "悪夢" not in text and "3本まで" not in text
    env.ws.values[2][1] = "YES"
    works.clear_cache()
    cmd(env, "プロジェクト")
    assert "3本までと決めていました" in env.sent[-1]


def test_now_set_is_refused_when_three_already(env):
    cmd(env, "今やる 13番")
    assert "3本そろっています" in env.sent[-1] and env.ws.updates == []


def test_now_unset_then_set(env):
    cmd(env, "今やる やめる 30")
    assert env.ws.values[3][1] == "-" and "外しました" in env.sent[-1]
    cmd(env, "今やる 悪夢")
    assert env.ws.values[2][1] == "YES" and env.ws.values[2][16] == "2026/10/16" and env.reactions == ["📂", "📂"]


def test_ambiguous_and_missing(env):
    cmd(env, "工程 駒")
    assert "番号で送ってください" in env.sent[-1] and "#47" in env.sent[-1]
    cmd(env, "工程 ない作品")
    assert env.sent[-1] == works_cmd.NOT_FOUND


def test_stage_show_set_and_next(env):
    cmd(env, "工程 4")
    assert "▶" in env.sent[-1] and "校正01" in env.sent[-1]
    assert (env.store.read(stages.path()) or "").startswith("# 工程テンプレート")  # 最初に既定で作る
    cmd(env, "次の工程 4番")
    assert env.ws.values[1][9] == "本文改稿01" and "タスク案 4" in env.sent[-1]
    cmd(env, "工程 4番 自由な段階")
    assert env.ws.values[1][9] == "自由な段階"
    cmd(env, "次の工程 4")
    assert "テンプレートにありません" in env.sent[-1] and env.ws.values[1][9] == "自由な段階"


def test_continuing_project_has_no_stages(env):
    cmd(env, "工程 46")
    assert "工程を持たない" in env.sent[-1]


def test_task_ideas_and_selection(env, monkeypatch):
    prompts = []

    async def fake(prompt, fallback, **kw):
        prompts.append(prompt)
        return {"tasks": ["導入のファイルを開く", "HO1導入を推敲", "NPCの台詞を見直す", "4つ目"]}

    monkeypatch.setattr(claude_client, "complete_json", fake)
    monkeypatch.setattr(works, "outline", lambda p: ["○午前", "・海の家"])
    cmd(env, "タスク案 4")
    assert "校正01" in prompts[0] and "○午前、・海の家" in prompts[0]
    assert "1. 導入のファイルを開く" in env.sent[-1] and "4つ目" not in env.sent[-1]
    mid = list(env.pending)[-1]
    assert cmd(env, "1, 3", ref=mid)
    assert env.added == []  # 日付のないタスクは増やさない（プロジェクトボード）
    assert env.ws.values[1][17] == "導入のファイルを開く" and "最初の番号" in env.sent[-1]
    assert env.pending == {} and env.reactions[-1] == "📂" and env.refreshed == 1


def test_task_ideas_none(env, monkeypatch):
    async def fake(prompt, fallback, **kw):
        return {"tasks": ["a"]}

    monkeypatch.setattr(claude_client, "complete_json", fake)
    monkeypatch.setattr(works, "outline", lambda p: [])
    cmd(env, "タスク案 4")
    assert cmd(env, "なし") and env.added == [] and env.pending == {}


def test_number_reply_without_pending_is_not_a_command(env):
    assert cmd(env, "1, 3") is False


def test_stop_and_resume(env):
    cmd(env, "止まった 4")
    assert "理由を一言" in env.sent[-1] and env.ws.updates == []
    cmd(env, "止まった 4番 戦闘バランスが不安")
    assert env.ws.values[1][10] == "戦闘バランスが不安"
    cmd(env, "再開 M.M.5s")
    assert env.ws.values[1][10] == "" and "おかえりなさい" in env.sent[-1]


def test_did_for_a_continuing_project(env):
    cmd(env, "やった 運動")
    content, kw = env.added[-1]
    assert content == "運動習慣をつける｜やった" and kw["done"] is True and kw["source"] == "作品#46"
    assert kw["done_date"] == "2026-10-16" and "今週 1回目" in env.sent[-1]
    assert env.ws.values[4][16] == "2026/10/16"


def test_sheet_unreadable(env, monkeypatch):
    def broken(refresh=False):
        raise RuntimeError("quota")

    monkeypatch.setattr(works, "load", broken)
    assert cmd(env, "今やる") and "読めませんでした" in env.sent[-1]


def test_project_room_does_not_record_commands(monkeypatch):
    async def handled(message, text):
        return True

    async def never(*a, **k):
        raise AssertionError("分類しない")

    monkeypatch.setattr(works_cmd, "handle", handled)
    monkeypatch.setattr(project, "_classify", never)
    run(project.handle_room("trpg", NS(content="今やる 4", attachments=[], channel=NS(id=1))))


def test_complete_touches_project(monkeypatch):
    touched = []
    monkeypatch.setattr(config, "WORKS_SHEET_ID", "book-id")
    monkeypatch.setattr(util, "activity_day", lambda at=None: DAY)
    monkeypatch.setattr(sheets, "complete_tasks", lambda ids, day=None: ["M.M.5s｜導入"])
    monkeypatch.setattr(sheets, "find_tasks", lambda ids: [{"id": "a", "source": "作品#4", "content": "M.M.5s｜導入"}])
    monkeypatch.setattr(sheets, "today_tasks", lambda day: [])
    monkeypatch.setattr(works, "touch_tasks", lambda ts: touched.extend(ts))
    monkeypatch.setattr(task_sync, "run_safely", lambda: None)
    monkeypatch.setattr(state, "latest_pending", lambda *a, **k: {"payload": {"items": [{"id": "a", "content": "M.M.5s｜導入"}]}})
    monkeypatch.setattr(state, "put_pending", lambda *a, **k: None)

    async def completed_on(day):
        return []

    import done_list
    monkeypatch.setattr(done_list, "completed_on", completed_on)

    async def send(text, reference=None):
        return NS(id=1)

    async def react(e):
        pass

    run(today_task.handle(NS(content="完了 1", channel=NS(id=1, send=send), add_reaction=react)))
    assert [t["id"] for t in touched] == ["a"]


# ---------------------------------------------------------------- プロジェクトボード（次やること・今日やる・やる日・締切）


def test_next_column_and_deadline_are_read(ws):
    ws.values[1][17], ws.values[1][18] = "導入を推敲", "2026/11/15"
    p = works.load(refresh=True)[0]
    assert (p["next"], p["deadline"]) == ("導入を推敲", "2026/11/15")
    with pytest.raises(ValueError):
        works.write(p, "テスト", **{works.COL_DEADLINE: "2026/12/01"})  # 締切は読むだけ


def test_set_next_and_clear(env):
    cmd(env, "次やること 4 導入を推敲")
    assert env.ws.values[1][17] == "導入を推敲" and env.ws.values[1][16] == "2026/10/16"
    assert "今日やる 4" in env.sent[-1] and env.refreshed == 1
    cmd(env, "次やること 4")
    assert "導入を推敲" in env.sent[-1]
    cmd(env, "次やること 4 なし")
    assert env.ws.values[1][17] == "" and "空にしました" in env.sent[-1]


def test_today_and_on_day_make_a_dated_task(env, monkeypatch):
    cmd(env, "今日やる 4")
    assert "先に次やることを決めましょう" in env.sent[-1] and env.added == []
    env.ws.values[1][17] = "導入を推敲"
    works.clear_cache()
    cmd(env, "今日やる 4")
    assert env.added == [("M.M.5s｜導入を推敲", {"scheduled": "2026-10-16", "source": "作品#4"})]
    updated = []
    monkeypatch.setattr(sheets, "update_task", lambda tid, **f: updated.append((tid, f)))
    env.tasks[-1].update(id="lo-x", deleted=False)
    cmd(env, "やる日 4 月")
    assert updated == [("lo-x", {"scheduled": "2026-10-19"})] and len(env.added) == 1  # 同じタスクは作らず、日を変える
    cmd(env, "やる日 4 そのうち")
    assert "読めませんでした" in env.sent[-1]


def test_completing_the_next_step_clears_it(ws):
    ws.values[1][17] = "導入を推敲"
    works.clear_cache()
    touched = works.touch_tasks([{"source": "作品#4", "content": "M.M.5s｜導入を推敲"}])
    assert [p["title"] for p in touched] == ["M.M.5s"] and ws.values[1][17] == ""
    ws.values[1][17] = "別のこと"
    works.clear_cache()
    works.touch_tasks([{"source": "作品#4", "content": "M.M.5s｜導入を推敲"}])
    assert ws.values[1][17] == "別のこと"  # 次やることと違うタスクなら消さない


def test_completion_reply_hint_once_a_day(monkeypatch):
    kv = {}
    monkeypatch.setattr(state, "get_kv", lambda k, d=None: kv.get(k, d))
    monkeypatch.setattr(state, "set_kv", lambda k, v: kv.__setitem__(k, v))
    p = {"title": "M.M.5s", "no": 4}
    assert "次やること 4" in today_task.next_hint([p], DAY)
    assert today_task.next_hint([p], DAY) is None


def test_morning_section_shows_next_and_first_milestone():
    ps = [{"title": "M.M.5s", "no": 4, "now": True, "kind": "TRPG", "stage": "校正01", "next": "導入を推敲",
           "deadline": "2026/11/15"}]
    assert works.morning_section(ps, [], DAY) == "📂 今やるプロジェクト：\n・M.M.5s（校正01） ／ 次: 導入を推敲 ／ 目標 11/1"
