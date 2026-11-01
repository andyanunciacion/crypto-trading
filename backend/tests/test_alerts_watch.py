"""Watching the engine's heartbeat (backend/alerts/watch.py, P7-3, D61).

The rules on fake clocks, then the watcher over a fake Redis that can be broken. The
service sending them, on the test database and Redis, is in test_alerts_service.py.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast

from redis.asyncio import Redis

from backend.alerts.format import render_engine
from backend.alerts.watch import (
    STATE_KEY,
    Change,
    EngineDown,
    EngineUp,
    EngineWatch,
    EngineWatcher,
    RedisDown,
    RedisUp,
    RedisWatch,
)
from shared.alerts import ENGINE_DOWN_SECONDS
from shared.heartbeat import ENGINE_HEARTBEAT_KEY, EngineHeartbeat

T0 = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)  # 14:00 in Manila
STARTED = T0 - timedelta(hours=5)


def beat(n: int, *, started_at: datetime = STARTED, ts: datetime | None = None) -> EngineHeartbeat:
    return EngineHeartbeat(
        ts=ts or started_at + timedelta(seconds=10 * n),
        started_at=started_at,
        mode="paper",
        beat=n,
        exchange="mexc",
        symbols=["BTC/USDT", "ETH/USDT"],
        bots=[1, 2, 3],
    )


def test_the_minute_counts_from_the_first_look_so_an_old_beat_left_behind_isnt_news() -> None:
    watch = EngineWatch(0)
    # Written hours ago by an engine that was already down when this service started.
    assert watch.observe(beat(7), 0) is None
    assert watch.observe(beat(7), 59.9) is None
    down = watch.observe(beat(7), 60)
    assert down == EngineDown(last=beat(7), silent_s=60)


def test_a_beating_engine_is_never_down_even_if_its_clock_is_far_off() -> None:
    watch = EngineWatch(0)
    for i in range(1, 100):
        # A beat dated a day ago, but changing every 10 s: the engine is alive.
        old = beat(i, ts=T0 - timedelta(days=1))
        assert watch.observe(old, i * 10.0) is None
    assert not watch.down


def test_down_once_then_quiet_then_up_at_the_next_new_beat() -> None:
    watch = EngineWatch(0)
    assert watch.observe(beat(1), 0) is None
    assert watch.observe(beat(2), 10) is None
    assert watch.observe(beat(2), 69.9) is None
    assert isinstance(watch.observe(beat(2), 70), EngineDown)
    watch.announced(True)
    assert all(watch.observe(beat(2), t) is None for t in (75.0, 500.0, 5000.0))

    up = watch.observe(beat(3), 5005)

    assert up == EngineUp(beat=beat(3), previous=beat(2), announced=True)
    assert not watch.down
    # And the minute starts again from that beat.
    assert watch.observe(beat(3), 5064) is None
    assert isinstance(watch.observe(beat(3), 5065), EngineDown)


def test_a_restarted_engine_counts_from_one_again_and_is_still_new() -> None:
    watch = EngineWatch(0)
    watch.observe(beat(40), 0)
    watch.observe(beat(40), 60)
    watch.announced(True)
    restarted = beat(1, started_at=T0)

    up = watch.observe(restarted, 300)

    assert up == EngineUp(beat=restarted, previous=beat(40), announced=True)


def test_up_says_whether_the_down_alert_went() -> None:
    watch = EngineWatch(0)
    watch.observe(beat(1), 0)
    watch.observe(beat(1), 60)
    watch.announced(False)  # turned off on the Settings page

    up = watch.observe(beat(2), 90)

    assert isinstance(up, EngineUp)
    assert not up.announced


def test_no_heartbeat_at_all_is_down_a_minute_after_starting() -> None:
    watch = EngineWatch(100)
    assert watch.observe(None, 100) is None
    assert watch.observe(None, 159) is None
    assert watch.observe(None, 160) == EngineDown(last=None, silent_s=60)
    # The first beat after that is the engine coming up.
    up = watch.observe(beat(1, started_at=T0), 170)
    assert up == EngineUp(beat=beat(1, started_at=T0), previous=None, announced=False)


def test_the_key_going_away_restarts_the_minute() -> None:
    watch = EngineWatch(0)
    watch.observe(beat(1), 0)
    assert watch.observe(None, 50) is None  # Redis emptied: no beat yet, but not news
    assert watch.observe(None, 109) is None
    assert isinstance(watch.observe(None, 110), EngineDown)


def test_an_outage_carries_over_a_restart_of_this_service() -> None:
    first = EngineWatch(0)
    first.observe(beat(5), 0)
    first.observe(beat(5), 60)
    first.announced(True)

    again = EngineWatch(1000, state=first.state())

    assert again.down
    assert again.observe(beat(5), 1000) is None  # the same beat: not said twice
    assert again.observe(beat(5), 9000) is None
    assert again.observe(beat(6), 9005) == EngineUp(beat=beat(6), previous=beat(5), announced=True)


def test_a_state_saved_while_up_is_a_fresh_start() -> None:
    first = EngineWatch(0)
    first.observe(beat(5), 0)

    again = EngineWatch(1000, state=first.state())

    assert not again.down
    assert again.observe(beat(5), 1000) is None
    assert again.observe(beat(5), 1059) is None  # counted from this run's first look
    assert isinstance(again.observe(beat(5), 1060), EngineDown)


def test_more_time_after_redis_was_unreachable() -> None:
    watch = EngineWatch(0)
    watch.observe(beat(1), 0)
    watch.give_time(100)  # Redis came back: the engine couldn't beat meanwhile
    assert watch.observe(beat(1), 159) is None
    assert isinstance(watch.observe(beat(1), 160), EngineDown)


def test_redis_down_after_a_minute_of_failed_reads_and_up_at_the_first_good_one() -> None:
    watch = RedisWatch()
    assert watch.ok(0) is None  # never failed: nothing to say
    assert watch.failed(10, "ConnectionError: refused") is None
    assert watch.failed(69, "ConnectionError: refused") is None
    assert watch.failed(70, "ConnectionError: refused") == RedisDown(
        silent_s=60, error="ConnectionError: refused"
    )
    assert watch.failed(75, "ConnectionError: refused") is None  # once
    watch.announced(True)
    assert watch.ok(100) == RedisUp(down_s=90, announced=True)
    assert not watch.failing


def test_a_short_redis_blip_says_nothing() -> None:
    watch = RedisWatch()
    assert watch.failed(0, "TimeoutError: ") is None
    assert watch.ok(30) is None
    assert watch.failed(40, "TimeoutError: ") is None  # a new minute from here
    assert watch.failed(99, "TimeoutError: ") is None


# --- The messages --------------------------------------------------------------------


def test_down_says_when_it_last_beat_what_that_means_and_what_to_check() -> None:
    text = render_engine(EngineDown(last=beat(30), silent_s=64.2), T0)
    assert text == (
        "Engine DOWN: no new heartbeat for 64 s\n"
        "Last beat 01 Nov 09:05, running since 01 Nov 09:00\n"
        "Bots aren't trading or watching their stops. "
        "Check: docker compose ps engine (and its logs).\n"
        "A /kill now stays queued until the engine starts again.\n"
        "01 Nov 14:00"
    )
    assert render_engine(EngineDown(last=None, silent_s=60), T0).splitlines()[1] == (
        "No heartbeat at all since the alerts service started"
    )


def test_up_says_how_long_and_whether_it_restarted() -> None:
    restarted = beat(1, started_at=T0 - timedelta(minutes=1))
    text = render_engine(EngineUp(beat=restarted, previous=beat(30), announced=True), T0)
    assert text.splitlines() == [
        "Engine back up, after 4 h without a heartbeat",
        "Restarted at 01 Nov 13:59",
        "Running 3 bots, streaming 2 symbols",
        "01 Nov 14:00",
    ]
    # Stuck, not restarted (the same start): no restart line.
    same = render_engine(EngineUp(beat=beat(40), previous=beat(30), announced=True), T0)
    assert same.splitlines()[:2] == [
        "Engine back up, after 100 s without a heartbeat",
        "Running 3 bots, streaming 2 symbols",
    ]


def test_redis_messages() -> None:
    down = render_engine(RedisDown(silent_s=61, error="ConnectionError: refused"), T0)
    assert down.splitlines()[:2] == [
        "Redis unreachable for 61 s: the engine's status is unknown",
        "ConnectionError: refused",
    ]
    up = render_engine(RedisUp(down_s=600, announced=True), T0)
    assert up == "Redis reachable again, after 10 min\n01 Nov 14:00"


# --- The watcher, over a fake Redis ----------------------------------------------------


@dataclass
class FakeRedis:
    values: dict[str, bytes] = field(default_factory=dict)
    broken: bool = False

    async def get(self, key: str) -> bytes | None:
        if self.broken:
            raise ConnectionError("Connection refused")
        return self.values.get(key)

    async def set(self, key: str, value: str) -> None:
        if self.broken:
            raise ConnectionError("Connection refused")
        self.values[key] = value.encode()

    def beat(self, b: EngineHeartbeat) -> None:
        self.values[ENGINE_HEARTBEAT_KEY] = b.model_dump_json().encode()


@dataclass
class FakeAnnouncer:
    on: bool = True
    changes: list[Change] = field(default_factory=list)

    async def send_engine(self, change: Change) -> bool:
        if self.on:
            self.changes.append(change)
        return self.on


@dataclass
class Mono:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


def watcher(redis: FakeRedis, announcer: FakeAnnouncer, mono: Mono) -> EngineWatcher:
    return EngineWatcher(announcer, cast(Redis, redis), monotonic=mono)


async def test_the_engine_stopping_and_starting_is_said_once_each() -> None:
    redis, announcer, mono = FakeRedis(), FakeAnnouncer(), Mono()
    w = watcher(redis, announcer, mono)
    for i in range(1, 4):  # beating every 10 s, looked at every 5 s
        redis.beat(beat(i))
        for _ in range(2):
            await w.step()
            mono.now += 5
    # The engine stops after beat 3, seen at t=20. Down at t=80.
    while mono.now < 80:
        await w.step()
        assert announcer.changes == []
        mono.now += 5
    await w.step()
    assert announcer.changes == [EngineDown(last=beat(3), silent_s=60)]
    await w.step()
    assert len(announcer.changes) == 1

    redis.beat(beat(1, started_at=T0))
    await w.step()

    assert announcer.changes[1] == EngineUp(
        beat=beat(1, started_at=T0), previous=beat(3), announced=True
    )


async def test_a_new_watcher_mid_outage_doesnt_repeat_and_still_says_its_back() -> None:
    redis, announcer, mono = FakeRedis(), FakeAnnouncer(), Mono()
    redis.beat(beat(3))
    w = watcher(redis, announcer, mono)
    await w.step()
    mono.now = ENGINE_DOWN_SECONDS
    await w.step()
    assert len(announcer.changes) == 1
    assert STATE_KEY in redis.values

    restarted = watcher(redis, announcer, Mono(5000))  # this service restarted
    await restarted.step()
    assert len(announcer.changes) == 1

    redis.beat(beat(4))
    await restarted.step()
    assert announcer.changes[1] == EngineUp(beat=beat(4), previous=beat(3), announced=True)


async def test_back_up_isnt_said_when_down_wasnt() -> None:
    redis, announcer, mono = FakeRedis(), FakeAnnouncer(on=False), Mono()
    redis.beat(beat(3))
    w = watcher(redis, announcer, mono)
    await w.step()
    mono.now = 60
    await w.step()
    announcer.on = True  # turned on again while it's down

    redis.beat(beat(4))
    await w.step()

    assert announcer.changes == []


async def test_redis_unreachable_is_said_and_the_engine_gets_a_new_minute_after() -> None:
    redis, announcer, mono = FakeRedis(), FakeAnnouncer(), Mono()
    redis.beat(beat(3))
    w = watcher(redis, announcer, mono)
    await w.step()
    redis.broken = True
    for t in range(5, 70, 5):
        mono.now = t
        await w.step()
    assert announcer.changes == [
        RedisDown(silent_s=60, error="ConnectionError: Connection refused")
    ]

    redis.broken = False
    mono.now = 100
    await w.step()  # the engine hasn't written its next beat yet: not down

    assert announcer.changes[1] == RedisUp(down_s=95, announced=True)
    mono.now = 159
    await w.step()
    assert len(announcer.changes) == 2
    mono.now = 160
    await w.step()
    assert isinstance(announcer.changes[2], EngineDown)


async def test_redis_unreachable_at_start_is_watched_too() -> None:
    redis, announcer, mono = FakeRedis(broken=True), FakeAnnouncer(), Mono()
    w = watcher(redis, announcer, mono)
    await w.step()
    mono.now = 60
    await w.step()
    assert isinstance(announcer.changes[0], RedisDown)

    redis.broken = False
    redis.beat(beat(1))
    mono.now = 70
    await w.step()
    assert announcer.changes[1:] == [RedisUp(down_s=70, announced=True)]


async def test_an_unreadable_beat_counts_as_none() -> None:
    redis, announcer, mono = FakeRedis(), FakeAnnouncer(), Mono()
    redis.values[ENGINE_HEARTBEAT_KEY] = b"{not json"
    w = watcher(redis, announcer, mono)
    await w.step()
    mono.now = 60
    await w.step()
    assert announcer.changes == [EngineDown(last=None, silent_s=60)]
