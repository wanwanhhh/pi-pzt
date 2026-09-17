"""_Link 的离线测试：假串口顶替 pyserial，不碰硬件。

_Link 是设备层里**唯一没有证人**的一层 —— 设备层的测试用假 link 把它整个换掉，
于是「帧间隔 50 ms」「按 want_b3 认回包」「超时返回 None」「drain 清干净」这些
策略谁都没测过。这里补上：给它一个假串口，它自己就成了被测对象。

直接跑：python backend/tests/test_xmt_link.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend import xmt_protocol as xp  # noqa: E402
from backend.xmt_stage import FRAME_GAP, _Link  # noqa: E402


class FakeSer:
    """假串口：主机一写帧，它就把预设的回包塞进收件箱 —— 像真设备那样应答。

    注意不能"预先把回包放进收件箱"：_Link.ask 第一件事就是 drain()，会把它清掉。
    extra 里可以预放"不是这次要的回包"（周期推送、上一条的迟到回包），
    用来验证 ask 会不会认错。
    """

    def __init__(
        self, replies: dict | None = None, extra: bytes = b"", corrupt: bool = False
    ) -> None:
        self.replies = dict(replies or {})   # b3 -> 数据段
        self.extra = extra
        self.corrupt = corrupt
        self.inbox = bytearray()
        self.written: list[bytes] = []
        self.baudrate = 115200
        self.closed = False
        self.resets = 0

    @property
    def in_waiting(self) -> int:
        return len(self.inbox)

    def read(self, n: int) -> bytes:
        out = bytes(self.inbox[:n])
        del self.inbox[:n]
        return out

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        self.inbox += self.extra
        req = xp.Parser().feed(bytes(data))
        if req:
            payload = self.replies.get(req[0].b3)
            if payload is not None:
                raw = bytearray(xp.frame(req[0].b3, payload, addr=req[0].addr))
                if self.corrupt:
                    raw[-1] ^= 0xFF
                self.inbox += bytes(raw)
        return len(data)

    def reset_input_buffer(self) -> None:
        self.resets += 1
        self.inbox.clear()

    def close(self) -> None:
        self.closed = True


def link_with(ser: FakeSer | None = None) -> tuple[_Link, FakeSer]:
    ser = ser or FakeSer()
    return _Link("FAKE", ser=ser), ser


def _data(value: float) -> bytes:
    return bytes((0,)) + xp.encode_value(value)


# ---------------- 认回包 ----------------

def test_ask_returns_the_frame_it_waits_for():
    link, ser = link_with(FakeSer({xp.CMD_MODEL: bytes((0x42,))}))
    f = link.ask(xp.frame(xp.CMD_MODEL), xp.CMD_MODEL, timeout=0.2)
    assert f is not None and f.b3 == xp.CMD_MODEL and f.data == bytes((0x42,))
    assert ser.written, "请求帧没发出去"


def test_ask_skips_other_replies():
    """流式回包 / 上一条的迟到回包会混进来，不能被当成答案。"""
    stray = xp.frame(xp.CMD_STREAM_POSITION, _data(1.0))
    link, _ = link_with(FakeSer({xp.CMD_READ_POSITION: _data(2.0)}, extra=stray))
    f = link.ask(xp.frame(xp.CMD_READ_POSITION, bytes((0,))), xp.CMD_READ_POSITION, timeout=0.2)
    assert f is not None and f.b3 == xp.CMD_READ_POSITION
    assert abs(f.value - 2.0) < 1e-9


def test_ask_returns_none_on_timeout():
    link, _ = link_with()          # 设备一声不吭
    t0 = time.monotonic()
    assert link.ask(xp.frame(xp.CMD_MODEL), xp.CMD_MODEL, timeout=0.08) is None
    assert time.monotonic() - t0 >= 0.07, "超时前就返回了"


def test_ask_drops_a_corrupt_frame():
    """校验错的帧要被解析器丢掉，不能当成回包。"""
    link, _ = link_with(FakeSer({xp.CMD_MODEL: bytes((0x42,))}, corrupt=True))
    assert link.ask(xp.frame(xp.CMD_MODEL), xp.CMD_MODEL, timeout=0.08) is None


# ---------------- 帧间隔 ----------------

def test_send_respects_the_frame_gap():
    """协议要求主机帧间隔 ≥50 ms —— 实测设备不强制，但生产端照手册来。"""
    link, ser = link_with()
    link.send(xp.frame(xp.CMD_MODEL))
    t0 = time.monotonic()
    link.send(xp.frame(xp.CMD_READ_UNIT))
    gap = time.monotonic() - t0
    assert len(ser.written) == 2
    assert gap >= FRAME_GAP * 0.9, f"两帧只隔了 {gap * 1000:.1f} ms"


def test_gap_is_measured_from_the_last_send_not_added_every_time():
    """是"距离上一次发送至少 50 ms"，不是"每次发送都睡 50 ms"。"""
    link, _ = link_with()
    link.send(xp.frame(xp.CMD_MODEL))
    time.sleep(FRAME_GAP + 0.02)
    t0 = time.monotonic()
    link.send(xp.frame(xp.CMD_READ_UNIT))
    assert time.monotonic() - t0 < FRAME_GAP * 0.5, "已经等够了还在等"


# ---------------- 状态清理 ----------------

def test_drain_clears_the_inbox_and_the_parser():
    link, ser = link_with()
    ser.inbox += xp.frame(xp.CMD_MODEL, bytes((0x42,)))[:4]   # 半包残留
    link.ask(xp.frame(xp.CMD_READ_UNIT), xp.CMD_READ_UNIT, timeout=0.02)
    assert len(ser.inbox) == 0
    assert link.parser._buf == bytearray(), "解析器里还留着半包"


def test_set_baud_updates_both_sides_and_drains():
    link, ser = link_with(FakeSer())
    ser.inbox += xp.frame(xp.CMD_MODEL)
    link.set_baud(9600)
    assert link.baud == 9600 and ser.baudrate == 9600
    assert ser.resets >= 1 and len(ser.inbox) == 0


def test_close_closes_the_port():
    link, ser = link_with()
    link.close()
    assert ser.closed is True


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
