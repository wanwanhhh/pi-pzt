"""xmt_stage 的离线单测：不碰硬件，不碰串口。

用一个假串口（FakeLink）顶替 pyserial，把「连接时读什么、设点写什么单位、
软停写回什么值」这三件事钉死 —— 单位陷阱是实测踩出来的：
读回 144.8785 原样写回设点，台子会跑到 144.95 µm（跑偏 4/3 倍）。

直接跑：python backend/tests/test_xmt_stage.py
"""
from __future__ import annotations

import inspect
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend import xmt_protocol as xp  # noqa: E402
from backend.stage_api import (  # noqa: E402
    RELEASE_OPEN_LOOP_ZERO,
    SETTLE_SOFTWARE,
    StageError,
    StageNotConnected,
    StageProto,
    StopResult,
)
from backend.xmt_stage import CAPS, SettleJudge, XmtStage  # noqa: E402

# 真机上量到的静止读回（此时真实位置 144.8785 × 0.75 = 108.6589 µm）
READBACK = 144.8785
POSITION_UM = READBACK * 0.75


def _data(value: float) -> bytes:
    """读命令的回包数据段：通道号 + 4 字节数值。"""
    return bytes((0,)) + xp.encode_value(value)


class FakeLink:
    """假串口：按 B3 查表回包，记下所有发出去的帧。"""

    def __init__(self, replies: dict | None = None, device_addr: int = 1) -> None:
        self.port = "FAKE1"
        self.addr = 1                    # 主机侧当前用的地址（设备层握手后会改它）
        self.device_addr = device_addr   # 设备自己的地址：只认它和广播
        self.baud = 115200
        self.sent: list[bytes] = []
        self.closed = False
        self._replies = {
            xp.CMD_HANDSHAKE: b"OK",
            xp.CMD_READ_LOOP_MODE: bytes((0,)) + b"C",
            xp.CMD_READ_UNIT: bytes((1,)),        # 单字节码，不是 4 字节数值
            xp.CMD_READ_POS_LIMIT_LOW: _data(0.0),
            xp.CMD_READ_POS_LIMIT_HIGH: _data(201.1799),
            xp.CMD_MODEL: bytes((0x42,)),        # 同上
            xp.CMD_READ_POSITION: _data(READBACK),
        }
        self._replies.update(replies or {})

    def close(self) -> None:
        self.closed = True

    def set_baud(self, baud: int) -> None:
        self.baud = baud

    def drain(self) -> None:
        pass

    def send(self, raw: bytes) -> None:
        self.sent.append(raw)

    def ask(self, raw: bytes, want_b3: int, timeout: float = 0.3):
        self.sent.append(raw)
        if raw[1] not in (self.device_addr, xp.BROADCAST):
            return None      # 不是发给我的：真设备就是这样静默丢弃
        data = self._replies.get(want_b3)
        if data is None:
            return None
        # 组帧再解析回来，跟真链路上走的是同一条路
        parsed = xp.Parser().feed(xp.frame(want_b3, data, addr=self.addr))  # type: ignore[arg-type]
        return parsed[0] if parsed else None


def frames(link: FakeLink) -> list[xp.Frame]:
    """把发出去的帧再解析回来（粘包也扛得住）。"""
    return xp.Parser().feed(b"".join(link.sent))


def setpoints(link: FakeLink) -> list[float]:
    return [
        xp.decode_value(f.data[1:5])
        for f in frames(link)
        if f.b3 == xp.CMD_SET_POSITION and len(f.data) >= 5
    ]


def connected(replies: dict | None = None) -> tuple[XmtStage, FakeLink]:
    link = FakeLink(replies)
    stage = XmtStage(link=link)
    stage.start()
    return stage, link


# ---------------- 形状与能力声明 ----------------

def test_satisfies_stage_protocol():
    assert isinstance(XmtStage(), StageProto)


def test_caps_declares_xmt_differences():
    assert CAPS.name == "芯明天 E53.D1S-H"
    assert CAPS.platform == "仅 Windows"
    assert CAPS.has_on_target is False, "设备没有到位信号"
    assert CAPS.has_stop_command is False, "设备没有停止指令"
    assert CAPS.has_setpoint_ack is False, "设点无应答，必须读回校验"
    assert CAPS.release_mode == RELEASE_OPEN_LOOP_ZERO
    assert CAPS.has_velocity is False, "本设备没有速度指令，能力声明里要说清楚"
    assert CAPS.unit == "µm"


def test_settle_source_is_software():
    """settle_source 必须显式写成 software：默认值指向不安全方向。"""
    assert XmtStage().status().settle_source == SETTLE_SOFTWARE


# ---------------- 连接 ----------------

def test_connect_reads_limits_and_units_from_device():
    stage, _ = connected()
    st = stage.status()
    assert st.connected is True
    assert st.servo is True
    assert (st.travel_min, st.travel_max) == (0.0, 201.1799)
    assert st.serial == "FAKE1"
    assert st.stage_type == "E53.D1S-H 型号码 0x42"
    stage.shutdown()


def test_open_loop_device_refuses_motion():
    stage, _ = connected({xp.CMD_READ_LOOP_MODE: bytes((0,)) + b"O"})
    assert stage.status().servo is False
    try:
        stage.move(10.0)
    except StageError:
        pass
    else:
        raise AssertionError("开环下不该接受位移设点")
    stage.shutdown()


def test_wrong_unit_code_is_rejected():
    try:
        connected({xp.CMD_READ_UNIT: bytes((4,))})   # 4 = mm
    except StageNotConnected as exc:
        assert "单位码" in str(exc)
    else:
        raise AssertionError("单位码不是 1（位移）时必须拒绝连接")


def test_suspicious_travel_is_rejected():
    try:
        connected({xp.CMD_READ_POS_LIMIT_HIGH: _data(5000.0)})
    except StageNotConnected as exc:
        assert "行程" in str(exc)
    else:
        raise AssertionError("行程读数不合理时必须拒绝连接")


def test_missing_limit_reply_is_rejected():
    try:
        connected({xp.CMD_READ_POS_LIMIT_HIGH: None})
    except StageError as exc:
        assert "27" in str(exc)
    else:
        raise AssertionError("27 没回包时必须拒绝连接")


# ---------------- 单位：读回折算、设点写 µm ----------------

def test_position_is_converted_to_um():
    stage, _ = connected()
    assert abs(stage.poll().position - POSITION_UM) < 1e-9
    stage.shutdown()


def test_setpoint_is_written_in_um():
    """设点是 µm：命令 100 µm 就写 100，不能乘 0.75。"""
    stage, link = connected()
    applied = stage.move(100.0)
    assert applied == 100.0
    assert setpoints(link)[-1] == 100.0
    stage.shutdown()


def test_soft_stop_writes_the_converted_position():
    """软停把**折算过的**位置写回成设点 —— 这条就是单位陷阱的回归测试。"""
    stage, link = connected()
    assert stage.stop_motion() is StopResult.SOFT
    value = setpoints(link)[-1]
    assert abs(value - POSITION_UM) < 1e-4, f"写回的必须是 µm：{value}"
    assert abs(value - READBACK) > 1.0, "把读回原样写回去了 —— 会跑偏 4/3 倍"
    stage.shutdown()


def test_clamp_uses_device_travel():
    stage, _ = connected()
    assert stage.clamp(-5.0) == 0.0
    assert stage.clamp(500.0) == 201.1799
    assert stage.clamp(12.5) == 12.5
    stage.shutdown()


def test_frames_use_the_negotiated_address():
    """握手认了地址 3，之后每一帧都得发给 3。

    xmt_protocol 的组帧 helper 把地址写死成 1，设备层必须在发出去之前改掉它，
    而且改地址就改了 BCC、必须重算。假串口只认自己的地址，就是用来钉这条的 ——
    实帧还会被 Parser 校验一次 BCC，改错了在这里就会露。
    """
    link = FakeLink(device_addr=3)
    stage = XmtStage(link=link, addr=3)
    stage.start()
    stage.move(100.0)
    addrs = {f.addr for f in frames(link)}
    assert addrs == {3}, f"帧地址应该是协商出来的 3，实际 {addrs}"
    stage.shutdown()


def test_open_loop_does_not_publish_position():
    """开环下读回的含义没有依据 —— 宁可什么都不发布，也不编一个数出来。"""
    stage, _ = connected({xp.CMD_READ_LOOP_MODE: bytes((0,)) + b"O"})
    st = stage.poll()
    assert st.servo is False
    assert st.on_target is False
    assert st.position == 0.0, f"开环下不该把读回折算成位置：{st.position}"
    stage.shutdown()


def test_signatures_match_the_contract():
    """Protocol 只查属性在不在、不查签名，这里把调用方真正用的形状钉死。"""
    assert list(inspect.signature(XmtStage.move).parameters) == ["self", "target"]
    assert list(inspect.signature(XmtStage.stop_motion).parameters) == ["self"]
    assert list(inspect.signature(XmtStage.wait_on_target).parameters) == [
        "self", "timeout", "cancel"]
    assert inspect.signature(XmtStage.wait_on_target).parameters["cancel"].default is None
    ann = inspect.signature(XmtStage.stop_motion).return_annotation
    assert ann in (StopResult, "StopResult"), ann


# ---------------- 停止 / 释放 / 速度 ----------------

def test_release_opens_loop_then_zeros_output():
    stage, link = connected()
    stage.release()
    tail = [f.b3 for f in frames(link)][-2:]
    assert tail == [xp.CMD_LOOP_MODE, xp.CMD_SET_VOLTAGE], (
        "必须先切开环再写零：闭环下写电压没有意义"
    )
    assert stage.status().servo is False
    stage.shutdown()


def test_hold_here_closes_loop_and_holds_position():
    stage, link = connected()
    pos = stage.hold_here()
    assert abs(pos - POSITION_UM) < 1e-9
    kinds = [f.b3 for f in frames(link)]
    i = len(kinds) - 1 - kinds[::-1].index(xp.CMD_LOOP_MODE)
    # 顺序是死的：先切闭环 → 读位移（开环下读回不是位移）→ 写成设点
    assert kinds[i + 1] == xp.CMD_READ_POSITION, "切闭环后要先读回位移"
    assert kinds[i + 2] == xp.CMD_SET_POSITION, "再把它写成设点，原地保持"
    assert xp.CMD_SET_POSITION not in kinds[:i], "不能先写设点再切闭环"
    assert abs(setpoints(link)[-1] - POSITION_UM) < 1e-4
    stage.shutdown()


def test_velocity_is_refused():
    stage, _ = connected()
    try:
        stage.set_velocity(100.0)
    except StageError as exc:
        assert "速度" in str(exc)
    else:
        raise AssertionError("本设备没有速度指令，必须明确拒绝而不是假装接受")
    stage.shutdown()


# ---------------- 停稳判据（纯逻辑） ----------------

def _judge() -> SettleJudge:
    return SettleJudge(window=5, need=2, eps_um=0.1, tol_um=0.5)


def test_judge_needs_need_plus_one_windows():
    j = _judge()
    j.reset(10.0)
    for i in range(5):                       # 第一窗：只立基线
        assert j.feed(10.0 + (0.01 if i % 2 else 0.0)) is False
    for _ in range(4):                       # 第二窗还没填满
        assert j.feed(10.0) is False
    assert j.feed(10.0) is True              # 第二窗确认


def test_judge_rejects_steady_motion():
    """匀速走必须被判成"还在动" —— 不重叠窗口才做得到。"""
    j = _judge()
    j.reset(100.0)
    settled = False
    for i in range(30):
        settled = j.feed(100.0 + 0.03 * i)   # 每样本 0.03 µm < ε，但一窗走 0.15 µm
    assert settled is False


def test_judge_rejects_far_from_target():
    """不动，但停在离目标很远的地方（设点丢了）→ 不能算到位。"""
    j = _judge()
    j.reset(50.0)
    for _ in range(20):
        settled = j.feed(10.0)
    assert settled is False


def test_judge_is_sticky_until_a_window_contradicts():
    j = _judge()
    j.reset(10.0)
    for _ in range(10):
        j.feed(10.0)
    assert j.feed(10.0) is True              # 窗口没满，保持上次结论
    for _ in range(5):
        j.feed(10.0 + 1.0)                   # 跳了 1 µm，远超 ε
    assert j.feed(10.0 + 1.0) is False


def test_judge_reset_forgets_the_past():
    j = _judge()
    j.reset(10.0)
    for _ in range(10):
        j.feed(10.0)
    assert j.feed(10.0) is True
    j.reset(80.0)
    assert j.feed(80.0) is False


def main() -> int:
    logging.disable(logging.CRITICAL)   # "设备当前是开环"那条警告是预期的
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
