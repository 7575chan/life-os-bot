"""昨日の執筆実績の投稿（scheduler.post_writing）。今日の行（09:44 に記録）が入ってから、1日1回だけ。"""
import asyncio
from datetime import date, datetime, time
from types import SimpleNamespace as NS

import pytest

import config
import scheduler
import util
import writing_log as wl

D0 = 46265.4  # 2026-08-31 09:36
HEADER = ["account@example.com", "港町の人魚", "空の作品"]
TODAY = date(2026, 9, 26)


def run(coro):
    return asyncio.run(coro)


def log_with(*totals, start=24):
    """9/(start) から1日1行。totals は港町の人魚の総文字数。"""
    rows = [[D0 + start + i, t, 0] for i, t in enumerate(totals)]
    return wl.parse([HEADER, ["目標", 30000, 1], ["締切", 46295, 46295], ["単位", 1, 1], ["id", "a", "b"], *rows])


class FakeChannel:
    def __init__(self, history=()):
        self.sent, self._history = [], list(history)

    async def send(self, text, **k):
        self.sent.append(text)
        return NS(id=len(self.sent))

    def history(self, limit=30):
        async def gen():
            for m in self._history:
                yield m

        return gen()


@pytest.fixture
def env(monkeypatch):
    kv = {}
    e = NS(kv=kv, ch=FakeChannel(), fetches=[], log=log_with(1000, 1500, 2100), hour=10, minute=10, fail_fetch=False)

    def fetch():
        e.fetches.append(1)
        if e.fail_fetch:
            raise RuntimeError("sheet down")
        return e.log

    monkeypatch.setattr(wl, "fetch", fetch)
    monkeypatch.setattr(scheduler.state, "get_kv", lambda key, default=None: kv.get(key, default))
    monkeypatch.setattr(scheduler.state, "set_kv", lambda key, value: kv.__setitem__(key, value))
    monkeypatch.setattr(scheduler, "_client", NS(user=NS(id=99)))
    monkeypatch.setattr(scheduler, "find_channel", lambda key: e.ch if key == "today" else None)
    monkeypatch.setattr(util, "today", lambda: TODAY)
    monkeypatch.setattr(util, "now", lambda: datetime(2026, 9, 26, e.hour, e.minute, tzinfo=config.TZ))
    monkeypatch.setattr(config, "WRITING_TIME", time(10, 0, tzinfo=config.TZ))
    return e


def test_writing_due_window():
    at = time(10, 0)
    d = lambda h, m: scheduler.writing_due(datetime(2026, 9, 26, h, m, tzinfo=config.TZ), at)
    assert not d(9, 59) and d(10, 0) and d(13, 59) and not d(14, 0)  # 10:00 から4時間以内
    assert not d(0, 5) and not d(23, 30)


def test_posts_yesterdays_count_once(env):
    assert run(scheduler.post_writing()) is True
    assert len(env.ch.sent) == 1 and env.ch.sent[0].startswith("📊 昨日の執筆実績\n") and "合計 +600文字" in env.ch.sent[0]  # 2100 − 1500
    assert "港町の人魚 +600（累計 2,100 / 目標 30,000・達成 7%" in env.ch.sent[0]
    n = len(env.fetches)
    assert run(scheduler.post_writing()) is False and len(env.ch.sent) == 1  # 1日1回
    assert len(env.fetches) == n  # 済みの日は、シートを読み直さない


def test_waits_until_the_writing_time_without_reading_the_sheet(env):
    env.hour, env.minute = 8, 0
    assert run(scheduler.post_writing()) is False and env.fetches == [] and env.ch.sent == []
    env.hour, env.minute = 14, 30  # 遅れすぎた時刻には、後追いで投稿しない
    assert run(scheduler.post_writing()) is False and env.fetches == []


def test_retries_until_todays_row_exists(env):
    env.log = log_with(1000, 1500)  # 9/24, 9/25（今日 9/26 の行がまだ無い）
    assert run(scheduler.post_writing()) is False and env.ch.sent == [] and "writing:2026-09-26" not in env.kv  # 済みにしない
    env.hour, env.minute = 10, 20
    env.log = log_with(1000, 1500, 2100)  # 09:44 の記録が遅れて入った
    assert run(scheduler.post_writing()) is True and "合計 +600文字" in env.ch.sent[0]


def test_never_calls_another_days_difference_yesterday(env):
    env.log = log_with(1000, 1500)
    run(scheduler.post_writing())
    assert env.ch.sent == []  # 9/25 の行 − 9/24 の行（+500）を、「昨日」として出さない


def test_no_growth_posts_nothing_but_is_not_checked_again(env):
    env.log = log_with(1000, 1500, 1500)
    assert run(scheduler.post_writing()) is False and env.ch.sent == []
    n = len(env.fetches)
    assert run(scheduler.post_writing()) is False and len(env.fetches) == n  # 今日の行は確認済み。10分ごとに読み直さない


def test_decrease_is_not_shown_as_negative(env):
    env.log = log_with(1000, 1500, 1200)  # 推敲で減った
    assert run(scheduler.post_writing()) is False and env.ch.sent == []


def test_skips_when_another_place_already_posted(env):
    env.ch = FakeChannel([NS(author=NS(id=99), content="📊 昨日の執筆実績\n・合計 +600文字", created_at=datetime(2026, 9, 26, 10, 1, tzinfo=config.TZ))])
    assert run(scheduler.post_writing()) is False and env.ch.sent == [] and env.kv["writing:2026-09-26"] == "1"


def test_sheet_unset_or_unreadable_does_nothing_and_retries(env):
    env.log = None
    assert run(scheduler.post_writing()) is False and env.ch.sent == []
    env.fail_fetch = True
    with pytest.raises(RuntimeError):
        run(scheduler.post_writing())  # 例外は writing_job が捕まえて、次の周期でやり直す
    assert "writing:2026-09-26" not in env.kv


def test_job_survives_a_failure(env):
    env.fail_fetch = True
    run(scheduler.writing_job.coro())  # 例外を外に出さない
    env.fail_fetch = False
    run(scheduler.writing_job.coro())
    assert len(env.ch.sent) == 1


def test_force_ignores_time_and_done_mark(env):
    env.hour = 7
    assert run(scheduler.post_writing(force=True)) is True and "writing:2026-09-26" not in env.kv
    assert run(scheduler.post_writing(force=True)) is True and len(env.ch.sent) == 2


def test_missing_channel_does_not_mark_done(env, monkeypatch):
    monkeypatch.setattr(scheduler, "find_channel", lambda key: None)
    assert run(scheduler.post_writing()) is False and "writing:2026-09-26" not in env.kv
