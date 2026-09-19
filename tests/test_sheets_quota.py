"""Sheets API の読み取り上限（1分あたり60回）を超えないように、無駄な読み取りをしないこと。

VM の Bot・タスク同期・手元での作業は、同じ Google Cloud プロジェクトの同じ枠を共有する。
"""
import gspread
import pytest

import sheets

HEADER = sheets.SCHEMA[sheets.TASKS]


class FakeWorksheet:
    def __init__(self, book, name):
        self.book, self.name, self.rows = book, name, [list(HEADER)]

    def row_values(self, n):
        self.book.calls["row_values"] += 1
        return list(self.rows[n - 1])

    def get_all_values(self, **kw):
        self.book.calls["get_all_values"] += 1
        return [list(r) for r in self.rows]

    def append_row(self, row, value_input_option=None):
        self.book.calls["append_row"] += 1
        self.rows.append(list(row))
        return {"updates": {"updatedRange": f"'{self.name}'!A{len(self.rows)}:I{len(self.rows)}"}}

    def batch_update(self, data, value_input_option=None):
        self.book.calls["batch_update"] += 1


class FakeBook:
    def __init__(self):
        self.calls = {"worksheet": 0, "row_values": 0, "get_all_values": 0, "append_row": 0, "batch_update": 0}
        self.sheets = {}

    def worksheet(self, name):
        self.calls["worksheet"] += 1
        return self.sheets.setdefault(name, FakeWorksheet(self, name))


@pytest.fixture
def book(monkeypatch):
    b = FakeBook()
    monkeypatch.setattr(sheets, "book", lambda: b)
    sheets.clear_caches()
    sheets._ok_sheets.add(sheets.TASKS)
    yield b
    sheets.clear_caches()


def test_adding_tasks_does_not_read_the_whole_sheet(book):
    for i in range(5):
        sheets.add_task(f"タスク{i}", source="test")
    assert book.calls["get_all_values"] == 0  # ID の重複確認のために、シート全体を読まない
    assert book.calls["append_row"] == 5
    assert book.calls["worksheet"] == 1  # タブの情報（1回の読み取り）は覚えておく
    assert book.calls["row_values"] == 1  # 見出し行も1回だけ


def test_task_ids_are_unique_without_reading():
    ids = {sheets.new_task_id() for _ in range(1000)}  # 8桁なら、1000個で重複する確率は 0.01% 未満
    assert len(ids) == 1000 and all(i.startswith("lo-") and len(i) == 11 for i in ids)
    assert sheets.new_task_id({"lo-x"}).startswith("lo-")  # 既存の ID を渡せば、それを避ける


def test_duplicate_ids_are_repaired_when_the_sheet_is_read(book):
    ws = sheets.ws(sheets.TASKS)
    row = lambda tid, c: [tid, "2026-09-20", c, "", "", "", "", "", ""]
    ws.rows += [row("lo-dup", "A"), row("lo-dup", "B")]
    tasks = sheets.all_tasks()
    assert len({t["id"] for t in tasks}) == 2  # 重複していた ID は付け直される


def test_reading_uses_one_request_per_call(book):
    sheets.add_task("a", source="t")
    before = dict(book.calls)
    sheets.all_tasks()
    sheets.today_tasks(__import__("datetime").date(2026, 9, 20))
    assert book.calls["get_all_values"] - before["get_all_values"] == 2  # 1回の関数につき1回
    assert book.calls["worksheet"] == before["worksheet"]  # タブの情報は再取得しない


def test_header_cache_refreshes_when_a_column_is_missing(book):
    sheets.add_task("a", source="t")
    ws = sheets.ws(sheets.TASKS)
    n = book.calls["row_values"]
    sheets.append(sheets.TASKS, {"内容": "b", "存在しない列": "x"})  # 見出しにない列 → 取り直して、無視して書く
    assert book.calls["row_values"] == n + 1
    assert ws.rows[-1][HEADER.index("内容")] == "b"


def test_ensure_schema_drops_the_caches(book):
    sheets.add_task("a", source="t")
    assert sheets._ws_cache and sheets._header_cache
    try:
        sheets.ensure_schema()
    except Exception:
        pass  # 偽のブックには worksheets() が無い。キャッシュが最初に捨てられることだけを確認する
    assert not sheets._ws_cache and not sheets._header_cache


def test_client_uses_the_backoff_http_client(monkeypatch):
    seen = {}
    monkeypatch.setattr(gspread, "service_account", lambda **kw: seen.update(kw) or object())
    monkeypatch.setattr(sheets, "_gc", None)
    sheets.client()
    assert seen["http_client"] is gspread.BackOffHTTPClient  # 429 のときは待って自動でやり直す
    sheets._gc = None
