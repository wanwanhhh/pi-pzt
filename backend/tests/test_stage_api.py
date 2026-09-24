"""薄接口层的形状自检：pi_stage.Stage 必须满足 stage_api.StageProto。

不连设备、不需要硬件。XMT 实现写完后也要过同一条。
直接跑：python backend/tests/test_stage_api.py
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:                                     # 3.13+ 的公开接口
    from typing import get_protocol_members as _members_of
except ImportError:                      # 老版本退回私有属性
    def _members_of(proto):
        return proto.__protocol_attrs__

from backend.config import DEFAULT_SETTLE_MS
from backend.pi_stage import CAPS, Stage
from backend.stage_api import (
    RELEASE_SERVO_OFF,
    SETTLE_DEVICE,
    SETTLE_SOFTWARE,
    StageProto,
    StageStatus,
    StopResult,
)


EXPECTED_MEMBERS = frozenset({
    "caps", "step_check", "start", "shutdown", "estop", "status", "poll", "clamp", "move", "jog",
    "set_servo", "hold_here", "set_velocity", "stop_motion", "release",
    "poll_on_target", "wait_on_target",
})


def test_protocol_members_are_exactly_as_expected():
    """写死集合。用 len(...) >= 15 那种护栏，删掉一个成员照样过。"""
    assert frozenset(_members_of(StageProto)) == EXPECTED_MEMBERS


def test_stage_satisfies_protocol():
    missing = sorted(n for n in _members_of(StageProto) if not hasattr(Stage, n))
    assert not missing, f"Stage 缺这些方法/属性: {missing}"


def test_signatures_used_by_callers():
    """只查名字不够：scanner 是用 cancel= 关键字调 wait_on_target 的，
    形状不对要到扫描中途才炸。"""
    sig = inspect.signature(Stage.wait_on_target)
    assert "cancel" in sig.parameters
    assert sig.parameters["cancel"].default is None
    assert sig.parameters["settle_s"].default == 0.0, "稳定延时必须能给，默认 0 = 不等"

    assert list(inspect.signature(Stage.move).parameters) == ["self", "target"]
    assert list(inspect.signature(Stage.stop_motion).parameters) == ["self"]


def test_stop_motion_declares_its_guarantee():
    """返回 None 的实现会让「软停」在调用方无从判断"""
    ann = inspect.signature(Stage.stop_motion).return_annotation
    assert ann in (StopResult, "StopResult"), ann


def test_pi_caps():
    assert CAPS.has_on_target and CAPS.has_stop_command and CAPS.has_setpoint_ack
    assert CAPS.has_velocity
    assert CAPS.release_mode == RELEASE_SERVO_OFF
    assert CAPS.default_settle_ms == DEFAULT_SETTLE_MS
    assert Stage.caps is CAPS


def test_step_check_is_declared_per_device():
    """采图前那道「半个步距」的相对校验做不做，由设备自己声明：

    PI 要（补「步距小于绝对容差时判据失效」的盲区）；**XMT 不要** —— 用户定的，
    等满稳定延时就直接采图，偏差一概不拦。代价见 docs/xmt/设备认识账.xml E12。
    """
    from backend.xmt_stage import XmtStage

    assert Stage.step_check is True, "PI 的到达容差是绝对的，这道相对校验必须留着"
    assert XmtStage.step_check is False, "XMT 不做任何到位判据（用户 2026-09 定）"


def test_pi_declares_device_settle():
    """PI 的到位是控制器给的硬件信号，要显式声明，不能靠默认值"""
    assert Stage()._status.settle_source == SETTLE_DEVICE


def test_settle_source_has_no_default():
    """故意不给默认值：给默认就会指向不安全方向，XMT 忘了设会自称「硬件到位」"""
    try:
        StageStatus()
    except TypeError:
        pass
    else:
        raise AssertionError("StageStatus 不该能在不给 settle_source 的情况下构造")
    got = StageStatus(settle_source=SETTLE_SOFTWARE).as_dict()["settle_source"]
    assert got == SETTLE_SOFTWARE


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
