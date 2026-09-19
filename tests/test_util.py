from datetime import date

import notes
import util


def test_parse_numbers():
    assert util.parse_numbers("1と3") == [1, 3]
    assert util.parse_numbers("1, 3") == [1, 3]
    assert util.parse_numbers("１、３") == [1, 3]
    assert util.parse_numbers("2") == [2]
    assert util.parse_numbers("今日は3つやる") is None
    assert util.parse_numbers("") is None


def test_none_choice():
    assert util.is_none_choice("なし")
    assert util.is_none_choice("０")
    assert not util.is_none_choice("1")


def test_parse_date_any():
    assert util.parse_date_any("2026-09-19") == date(2026, 9, 19)
    assert util.parse_date_any("2026/9/1 10:00") == date(2026, 9, 1)
    assert util.parse_date_any("") is None
    assert util.parse_date_any("2026-13-40") is None


def test_is_stale():
    d = date(2026, 9, 19)
    assert util.is_stale("2026-09-16", d)
    assert not util.is_stale("2026-09-17", d)
    assert not util.is_stale("", d)


def test_extract_json():
    assert util.extract_json('前置き {"a": 1} 後') == {"a": 1}
    assert util.extract_json('```json\n[1,2]\n```') == [1, 2]
    assert util.extract_json("json無し") is None


def test_split_message():
    text = "\n".join(["あ" * 100] * 50)
    chunks = util.split_message(text, 1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_month_helpers():
    assert util.month_range(date(2026, 12, 15)) == (date(2026, 12, 1), date(2027, 1, 1))
    assert util.prev_month_start(date(2026, 1, 10)) == date(2025, 12, 1)
    assert util.is_last_day_of_month(date(2026, 2, 28))
    assert not util.is_last_day_of_month(date(2026, 2, 27))


def test_insert_below_header_newest_first():
    t = notes.insert_below_header("", "## 2026-09-19 10:00\n#日常 一つ目", "Ideas")
    t = notes.insert_below_header(t, "## 2026-09-19 11:00\n#仕事 二つ目", "Ideas")
    assert t.startswith("# Ideas\n\n## 2026-09-19 11:00")
    assert t.index("二つ目") < t.index("一つ目")


def test_insert_below_header_keeps_frontmatter():
    t = notes.insert_below_header("---\ntags: x\n---\n\n# T\n\n古い\n", "新しい", "T")
    assert t.startswith("---\ntags: x\n---")
    assert t.index("新しい") < t.index("古い")


def test_replace_title_and_meta():
    assert notes.replace_title("# 旧\n本文", "新").startswith("# 新")
    t = notes.set_meta_line("---\ntags: #a\n---\n本文", "tags", "#b #c")
    assert "tags: #b #c" in t and "#a" not in t
