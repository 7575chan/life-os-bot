import asyncio
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

import claude_client
import config
import notes
import settings
import sheets
import state
import util
import vault_paths
import webfetch
from handlers import scrap

NOW = datetime(2026, 9, 20, 10, 30, tzinfo=config.TZ)
URL = "https://example.com/post"


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 純粋関数


def test_parse_tag_edit_modes():
    assert scrap.parse_tag_edit("これに #イラスト のタグも追加して") == ("add", ["イラスト"])
    assert scrap.parse_tag_edit("#イラスト を追加") == ("add", ["イラスト"])
    assert scrap.parse_tag_edit("タグを #設定 に変更して") == ("set", ["設定"])
    assert scrap.parse_tag_edit("#設定 だけにして") == ("set", ["設定"])
    assert scrap.parse_tag_edit("#AI を削除") == ("remove", ["AI"])
    assert scrap.parse_tag_edit("#AI と #古い は外して") == ("remove", ["AI", "古い"])
    assert scrap.parse_tag_edit("＃全角 を足して") == ("add", ["全角"])
    assert scrap.parse_tag_edit("#新 を追加して、タグを #旧 に変更") == ("add", ["新", "旧"])  # 追加と書かれていれば追加を優先
    assert scrap.parse_tag_edit("タグを直したい") is None  # ハッシュタグが無い


def test_apply_tag_edit():
    assert scrap.apply_tag_edit(["a", "b"], "add", ["c", "a"]) == ["a", "b", "c"]
    assert scrap.apply_tag_edit(["a", "b"], "set", ["x"]) == ["x"]
    assert scrap.apply_tag_edit(["a", "b"], "remove", ["a", "zzz"]) == ["b"]
    assert scrap.apply_tag_edit(["a"], "remove", ["a"]) == []
    assert len(scrap.apply_tag_edit([str(i) for i in range(9)], "add", ["x", "y", "z"])) == scrap.MAX_TAGS


def test_clean_tags_and_written_tags():
    assert scrap.clean_tags(["#AI", " デザイン ", "AI", "", "a b"], 5) == ["AI", "デザイン", "ab"]
    assert scrap.clean_tags(["#AI", "AI", " デザイン "]) == ["AI", "デザイン"]
    assert scrap.clean_tags("壊れた") == [] and scrap.clean_tags(None) == []
    assert scrap.written_tags("https://example.com/a#section #デザイン #AI") == ["デザイン", "AI"]  # URL の #断片は除く


def test_summary_lines_cleaning_and_limit():
    assert scrap.summary_lines(["・一行目 ", "- 二行目", "", "三行目", "四行目"], 3) == ["一行目", "二行目", "三行目"]
    assert scrap.summary_lines("壊れた", 3) == [] and scrap.summary_lines(None, 3) == []


def test_render_note_and_retag_roundtrip():
    text = scrap.render_note(title='引用"入り"タイトル', url=URL, day="2026-09-20", tags=["AI", "設計"], summary=["要約1", "要約2"],
                             body="本文です。", image_name="eye.png", notes_=["注意書き"])
    assert text.startswith('---\ntitle: "引用\'入り\'タイトル"\nurl: https://example.com/post\ndate: 2026-09-20\ntags: [AI, 設計]\n---\n')
    assert "# 引用\"入り\"タイトル" in text and "タグ: #AI #設計" in text and "![[eye.png]]" in text and "> ⚠️ 注意書き" in text
    assert "## 3行要約\n\n- 要約1\n- 要約2" in text and "## 本文\n\n本文です。" in text
    new = scrap.retag_note(text, ["イラスト"])
    assert "tags: [イラスト]" in new and "タグ: #イラスト" in new and "#AI" not in new
    assert new.replace("tags: [イラスト]", "tags: [AI, 設計]").replace("タグ: #イラスト", "タグ: #AI #設計") == text  # タグ以外は変わらない


def test_body_tags_line_is_not_confused_with_body_text():
    text = scrap.render_note(title="T", url=URL, day="2026-09-20", tags=["a"], summary=[], body="タグ: #本文中のタグ\ntags: 本文", image_name=None, notes_=[])
    new = scrap.retag_note(text, ["b"])
    assert "タグ: #b" in new and "タグ: #本文中のタグ" in new and "tags: 本文" in new  # 先頭のメタデータだけを書き換える


def test_format_reply_and_note_name():
    r = scrap.format_reply("題名", ["a", "b"], ["x", "y"], ["取得の注意"])
    assert r.startswith("📰 題名\n・a\n・b\n⚠️ 取得の注意\nタグ: #x #y\n") and "返信してください" in r
    assert scrap.note_name("2026-09-20", 'a/b:c*?"<>|') == "2026-09-20_abc"


# ---------------------------------------------------------------- 処理（偽の Web・AI・シート、実際のファイル）


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "06-Life-OS" / "08-scrap").mkdir(parents=True)
    store = notes.GuardedStore(notes.LocalStore(tmp_path))
    e = NS(root=tmp_path, store=store, rows=[], updates=[], pending={}, sent=[], fetched=[], images=[],
           article=webfetch.Article(title="元のタイトル", text="本文の全文です。" * 20, image_url="https://cdn.example.net/eye.png",
                                    final_url=URL),
           fetch_error=None, ai={"title": "AIのタイトル", "summary": ["要約1", "要約2", "要約3"], "tags": ["AI", "設計", "Discord"]},
           sheet_fail=False, image_fail=False)

    def fake_fetch(url, *a, **k):
        e.fetched.append(url)
        if e.fetch_error:
            raise e.fetch_error
        return e.article

    def fake_image(url, *a, **k):
        if e.image_fail:
            raise webfetch.FetchError("画像を取得できません")
        e.images.append(url)
        return b"\x89PNG-bytes", "image/png"

    async def fake_ai(prompt, fallback, **kw):
        e.ai_prompt = prompt
        return e.ai if e.ai is not None else fallback

    def fake_append(name, data):
        if e.sheet_fail:
            raise RuntimeError("sheet down")
        e.rows.append((name, data))
        return 2

    monkeypatch.setattr(webfetch, "fetch_article", fake_fetch)
    monkeypatch.setattr(webfetch, "fetch_image", fake_image)
    monkeypatch.setattr(claude_client, "complete_json", fake_ai)
    monkeypatch.setattr(notes, "get_store", lambda: store)
    monkeypatch.setattr(sheets, "append", fake_append)
    monkeypatch.setattr(sheets, "records", lambda name: [(i + 2, dict(d)) for i, (n, d) in enumerate(e.rows)])
    monkeypatch.setattr(sheets, "update_row", lambda name, row, data: e.updates.append((row, data)))
    monkeypatch.setattr(settings, "get", lambda key: {"summary_length": "3行", "scrap_tag_count": "3〜5個"}.get(key))
    monkeypatch.setattr(util, "now", lambda: NOW)
    monkeypatch.setattr(state, "put_pending", lambda mid, cid, kind, payload: e.pending.__setitem__(mid, {
        "message_id": mid, "channel_id": cid, "kind": kind, "payload": payload}))
    monkeypatch.setattr(state, "get_pending", lambda mid: e.pending.get(mid))
    counter = {"n": 100}

    async def send(text, reference=None):
        e.sent.append(text)
        counter["n"] += 1
        return NS(id=counter["n"])

    e.channel = NS(id=7, send=send)

    class Msg:
        def __init__(self, content, reference=None):
            self.content, self.channel, self.reference, self.reactions = content, e.channel, reference, []

        async def add_reaction(self, emoji):
            self.reactions.append(emoji)

    e.msg = Msg
    e.note = lambda: next((p for p in (tmp_path / "06-Life-OS" / "08-scrap").glob("*.md")), None)
    return e


def notes_in(env):
    return sorted((env.root / "06-Life-OS" / "08-scrap").glob("*.md"))


def test_saves_note_sheet_and_reply(env):
    m = env.msg(f"{URL} を保存")
    run(scrap.handle(m))
    (note,) = notes_in(env)
    assert note.name == "2026-09-20_AIのタイトル.md"
    text = note.read_text(encoding="utf-8")
    assert 'title: "AIのタイトル"' in text and f"url: {URL}" in text and "tags: [AI, 設計, Discord]" in text
    assert "## 3行要約\n\n- 要約1\n- 要約2\n- 要約3" in text and "本文の全文です。" in text
    assert "![[2026-09-20_AIのタイトル.png]]" in text  # アイキャッチ
    img = env.root / "06-Life-OS" / "08-scrap" / "attachments" / "2026-09-20_AIのタイトル.png"
    assert img.read_bytes() == b"\x89PNG-bytes"
    name, row = env.rows[0]
    assert name == "08-scrap" and row["タイトル"] == "AIのタイトル" and row["URL"] == URL
    assert row["3行要約"] == "要約1\n要約2\n要約3" and row["タグ"] == "#AI #設計 #Discord" and row["Obsidianリンク"]
    assert env.sent[0].startswith("📰 AIのタイトル\n・要約1") and "タグ: #AI #設計 #Discord" in env.sent[0]
    assert m.reactions == ["📰"]
    (pend,) = env.pending.values()
    assert pend["kind"] == "scrap" and pend["payload"]["url"] == URL and pend["payload"]["tags"] == ["AI", "設計", "Discord"]


def test_written_tags_override_the_ai_tags(env):
    run(scrap.handle(env.msg(f"{URL} #デザイン #AI")))
    assert "tags: [デザイン, AI]" in notes_in(env)[0].read_text(encoding="utf-8")
    assert env.rows[0][1]["タグ"] == "#デザイン #AI"
    assert "先頭の # は付けない" in env.ai_prompt  # AI は呼ぶが、タグは指定を優先


def test_ai_unavailable_falls_back_gracefully(env):
    env.ai = None
    run(scrap.handle(env.msg(URL)))
    text = notes_in(env)[0].read_text(encoding="utf-8")
    assert "title: \"元のタイトル\"" in text and "tags: [スクラップ]" in text and "## 3行要約\n\n- 本文の全文です。" in text


def test_fetch_failure_saves_url_only_and_warns(env):
    env.fetch_error = webfetch.FetchError("ページを取得できませんでした（HTTP 403）")
    m = env.msg(URL)
    run(scrap.handle(m))
    text = notes_in(env)[0].read_text(encoding="utf-8")
    assert "> ⚠️ ページを取得できませんでした（HTTP 403）。URL だけ保存しました" in text and "## 本文" not in text
    assert "title: \"example.com\"" in text
    assert m.reactions == ["📰", "⚠️"] and "⚠️ ページを取得できませんでした" in env.sent[0]
    assert env.rows[0][1]["URL"] == URL  # シートにも記録される


def test_unsafe_urls_are_refused_not_saved(env):
    env.fetch_error = webfetch.UnsafeUrl("内部のアドレスの URL は保存できません")
    m = env.msg("http://169.254.169.254/latest/meta-data/")
    run(scrap.handle(m))
    assert notes_in(env) == [] and env.rows == [] and env.pending == {}
    assert m.reactions == ["🚫"] and env.sent == ["🚫 この URL は保存できません: 内部のアドレスの URL は保存できません"]


def test_eyecatch_failure_does_not_stop_the_article(env):
    env.image_fail = True
    m = env.msg(URL)
    run(scrap.handle(m))
    assert "![[" not in notes_in(env)[0].read_text(encoding="utf-8") and m.reactions == ["📰"]


def test_no_url_gets_a_hint_and_records_nothing(env):
    run(scrap.handle(env.msg("面白い記事だった #デザイン")))
    assert env.rows == [] and notes_in(env) == [] and "URL を貼ると" in env.sent[0]


def test_multiple_urls_are_saved_one_by_one_up_to_three(env):
    urls = [f"https://example.com/p{i}" for i in range(5)]
    run(scrap.handle(env.msg(" ".join(urls))))
    assert env.fetched == urls[:3] and len(env.rows) == 3
    assert any("一度に3件まで" in s for s in env.sent)


def test_same_title_on_the_same_day_gets_a_suffix(env):
    run(scrap.handle(env.msg("https://example.com/a")))
    run(scrap.handle(env.msg("https://example.com/b")))
    assert [p.name for p in notes_in(env)] == ["2026-09-20_AIのタイトル-2.md", "2026-09-20_AIのタイトル.md"]


def test_obsidian_failure_records_nothing_else(env, monkeypatch):
    monkeypatch.setattr(env.store, "write", lambda *a, **k: (_ for _ in ()).throw(OSError("drive down")))
    m = env.msg(URL)
    with pytest.raises(OSError):
        run(scrap.handle(m))
    assert env.rows == [] and env.sent == [] and m.reactions == [] and env.pending == {}


def test_sheet_failure_keeps_the_note_and_warns(env):
    env.sheet_fail = True
    m = env.msg(URL)
    run(scrap.handle(m))
    assert len(notes_in(env)) == 1 and m.reactions == ["📰", "⚠️"] and env.sent


# ---------------------------------------------------------------- 返信でタグを直す


def saved(env, url=URL):
    run(scrap.handle(env.msg(url)))
    (mid,) = [k for k, v in env.pending.items() if v["payload"]["url"] == url]
    return NS(message_id=mid)


def test_reply_adds_sets_and_removes_tags_in_note_and_sheet(env):
    ref = saved(env)
    m = env.msg("#イラスト のタグも追加して", ref)
    run(scrap.handle(m))
    text = notes_in(env)[0].read_text(encoding="utf-8")
    assert "tags: [AI, 設計, Discord, イラスト]" in text and "タグ: #AI #設計 #Discord #イラスト" in text
    assert env.updates[-1] == (2, {"タグ": "#AI #設計 #Discord #イラスト"})
    assert m.reactions == ["🏷️"] and env.sent[-1] == "タグを更新しました：#AI #設計 #Discord #イラスト"
    run(scrap.handle(env.msg("タグを #設定 に変更して", ref)))
    assert "tags: [設定]" in notes_in(env)[0].read_text(encoding="utf-8") and env.updates[-1][1] == {"タグ": "#設定"}
    run(scrap.handle(env.msg("#設定 を削除", ref)))
    assert "tags: []" in notes_in(env)[0].read_text(encoding="utf-8") and env.updates[-1][1] == {"タグ": ""}
    assert env.sent[-1] == "タグを更新しました：なし"


def test_edit_state_persists_across_replies(env):
    ref = saved(env)
    run(scrap.handle(env.msg("#a を追加", ref)))
    run(scrap.handle(env.msg("#b を追加", ref)))
    assert env.pending[ref.message_id]["payload"]["tags"] == ["AI", "設計", "Discord", "a", "b"]


def test_reply_without_hashtags_asks_again_and_changes_nothing(env):
    ref = saved(env)
    before = notes_in(env)[0].read_text(encoding="utf-8")
    run(scrap.handle(env.msg("タグを直したい", ref)))
    assert notes_in(env)[0].read_text(encoding="utf-8") == before and env.updates == []
    assert "# 付きで教えてください" in env.sent[-1]


def test_reply_to_an_unrelated_message_is_treated_as_a_new_post(env):
    run(scrap.handle(env.msg(f"{URL} #新規", NS(message_id=999999))))
    assert len(env.rows) == 1  # 保存待ちの状態が無い返信は、通常の投稿として扱う


def test_reply_when_the_note_was_moved(env):
    ref = saved(env)
    notes_in(env)[0].unlink()
    run(scrap.handle(env.msg("#a を追加", ref)))
    assert "見つかりませんでした" in env.sent[-1] and env.updates == []


def test_only_the_scrap_room_is_written(env):
    saved(env)
    files = {p.relative_to(env.root).as_posix() for p in env.root.rglob("*") if p.is_file()}
    assert all(f.startswith("06-Life-OS/08-scrap/") for f in files), files
