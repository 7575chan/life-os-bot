"""メモリ上の偽のシート（Google スプレッドシートを使わずに、`sheets` の読み書きを差し替える）。"""
import sheets


class FakeSheets:
    def __init__(self):
        self.data = {name: [] for name in sheets.SCHEMA}
        self.calls: list[tuple] = []
        self.sync_calls = 0

    def install(self, monkeypatch) -> "FakeSheets":
        for fn in ("records", "append", "update_row", "delete_row", "insert_row", "add_task"):
            monkeypatch.setattr(sheets, fn, getattr(self, fn))
        return self

    def _blank(self, name: str, data: dict) -> dict:
        row = {c: "" for c in sheets.SCHEMA[name]}
        for k, v in data.items():
            if k not in row:
                raise KeyError(k)
            row[k] = v
        return row

    def seed(self, name: str, *rows: dict) -> None:
        self.data[name] += [self._blank(name, r) for r in rows]

    def records(self, name):
        return [(i + 2, dict(r)) for i, r in enumerate(self.data[name])]

    def append(self, name, data):
        self.calls.append(("append", name))
        self.data[name].append(self._blank(name, data))
        return len(self.data[name]) + 1

    def update_row(self, name, row, data):
        self.calls.append(("update_row", name, row))
        self.data[name][row - 2].update(self._blank(name, data) and {k: v for k, v in data.items()})

    def delete_row(self, name, row):
        self.calls.append(("delete_row", name, row))
        self.data[name].pop(row - 2)

    def insert_row(self, name, row, data):
        self.calls.append(("insert_row", name, row))
        self.data[name].insert(row - 2, self._blank(name, data))

    def add_task(self, content, scheduled="", due="", priority="", source="", **kw):
        tid = f"lo-{len(self.data[sheets.TASKS]):08d}"
        self.calls.append(("add_task", content, source))
        self.data[sheets.TASKS].append(self._blank(sheets.TASKS, {"ID": tid, "内容": content, "実行予定日": scheduled, "期限": due,
                                                                  "優先度": priority, "出典": source}))
        return tid

    def log_entries(self) -> list[dict]:
        return self.data[sheets.OPLOG]
