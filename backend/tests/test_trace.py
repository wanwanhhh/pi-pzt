"""位置曲线环形缓冲的离线自检：窗口两端、按时间裁剪、配置是否盖得住。

不碰硬件、不碰数据库。直接跑：python backend/tests/test_trace.py
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.config import TRACE_BUFFER_S, TRACE_MAX_S, TRACE_MIN_S
from backend.trace import TraceBuffer


def test_window_is_closed_on_both_ends():
    b = TraceBuffer(span_s=10.0)
    for i in range(5):
        b.add(100.0 + i, float(i), 0.0)
    got = [it[0] for it in b.window(101.0, 103.0)]
    assert got == [101.0, 102.0, 103.0], got


def test_window_drops_samples_after_to():
    """记录结束后前端还会再取一次，那次可能迟到一分钟：
    上界必须挡住 to 之后攒进来的样本，否则曲线会越画越长、标准差也是错的。"""
    b = TraceBuffer(span_s=60.0)
    for i in range(10):
        b.add(100.0 + i, float(i), 0.0)
    got = [it[0] for it in b.window(100.0, 103.0)]
    assert got == [100.0, 101.0, 102.0, 103.0], got


def test_window_is_empty_when_anchor_is_ahead_of_samples():
    """轮询线程卡住时锚点会比缓冲里最新的样本还新，这种情况给空，不能报错。"""
    b = TraceBuffer(span_s=10.0)
    b.add(100.0, 1.0, 1.0)
    assert b.window(101.0, 100.0) == []


def test_empty_buffer_yields_nothing():
    assert TraceBuffer(span_s=10.0).window(0.0, 1e9) == []


def test_trims_by_time_not_by_count():
    """按条数裁的话，扫描中遥测从 10 Hz 降到 2 Hz，缓冲会莫名其妙只剩几秒。"""
    b = TraceBuffer(span_s=10.0)
    for ts in (0.0, 9.0, 9.5, 19.5):
        b.add(ts, 0.0, 0.0)
    assert [it[0] for it in b.window(-1e9, 1e9)] == [9.5, 19.5], len(b)


def test_growth_is_bounded():
    """10 Hz 跑一小时也只有 span 秒的样本，别让它无限长。"""
    b = TraceBuffer(span_s=120.0)
    for i in range(36000):
        b.add(i * 0.1, 0.0, 0.0)
    assert len(b) <= 1200 + 1, len(b)


def test_config_covers_the_longest_recording_and_a_late_fetch():
    """缓冲要同时装得下：最长的一次记录 + 浏览器把后台标签的定时器压到 1 次/分钟
    之后迟到的最后一次取数（那一取要拿回记录开头）。"""
    assert TRACE_BUFFER_S >= TRACE_MAX_S + 60.0, (TRACE_BUFFER_S, TRACE_MAX_S)
    assert 0 < TRACE_MIN_S <= 10.0 <= TRACE_MAX_S, (TRACE_MIN_S, TRACE_MAX_S)


def test_concurrent_read_and_write_do_not_raise():
    """读窗口是 for 迭代这个 deque，写侧在 append/popleft —— 不在同一把锁里就会抛
    RuntimeError: deque mutated during iteration（实测约 0.01%/次读，遥测 10 Hz 一直在写）。
    这里真起一个写线程压一会儿：只要锁还在，这个测试永远不会失败。"""
    b = TraceBuffer(span_s=1.0)
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            b.add(time.time(), 1.0, 2.0)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        for _ in range(300):
            b.window(0.0, time.time() + 1.0)
            len(b)
    finally:
        stop.set()
        t.join(timeout=2.0)


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"FAIL  {name}\n      {type(exc).__name__}: {exc}")
        else:
            print(f"ok    {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
