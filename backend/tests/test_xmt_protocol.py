"""xmt_protocol 的离线单测：手册官方样例逐字节比对 + 解析器行为。

不碰串口，不发任何东西。直接跑：

    python backend/tests/test_xmt_protocol.py

装了 pytest 也照样能被收集（全是 test_ 开头的函数）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend import xmt_protocol as xp


# ---------------- 官方样例（docs/xmt/protocol/） ----------------

def test_golden_set_voltage():
    """0 通道发 10.001 V：aa 01 0b 00 00 00 00 0a 00 0a a0"""
    assert xp.set_voltage(10.001, 0).hex() == "aa010b000000000a000aa0"


def test_golden_set_position():
    """同值走位移：只有 B3 从 00 变 01，BCC 从 a0 变 a1"""
    assert xp.set_position(10.001, 0).hex() == "aa010b010000000a000aa1"


def test_golden_read_model():
    """78 读型号，无数据段。docs/xmt/README.md 给的例子"""
    assert xp.frame(xp.CMD_MODEL).hex() == "aa01064e00e3"


def test_golden_read_unit():
    """53 读单位，无数据段"""
    assert xp.frame(xp.CMD_READ_UNIT).hex() == "aa0106350098"


def test_golden_handshake():
    """77 连接检测"""
    assert xp.frame(xp.CMD_HANDSHAKE).hex() == "aa01064d00e0"


def test_golden_read_loop_mode():
    """19 读开闭环，数据段只有通道号"""
    assert xp.read_channel(xp.CMD_READ_LOOP_MODE, 0).hex() == "aa0107130000bf"


def test_golden_limits():
    """27 读高限 / 35 读低限"""
    assert xp.read_channel(xp.CMD_READ_POS_LIMIT_HIGH, 0).hex() == "aa01071b0000b7"
    assert xp.read_channel(xp.CMD_READ_POS_LIMIT_LOW, 0).hex() == "aa01072300008f"


def test_golden_diagnostics():
    """47 读地址必须用广播地址（指令表原文：[1]地址字节为0x00）

    0x2F：指令表里的「47」是**十进制** —— 写成 0x47 是另一条命令，
    这条曾经写错，README 里「47 无回包 ✗」的结论就是这么来的。
    """
    assert xp.CMD_READ_ADDRESS == 0x2F
    assert xp.frame(xp.CMD_READ_ADDRESS, addr=0).hex() == "aa00062f0083"
    assert xp.frame(xp.CMD_POWER_INFO).hex() == "aa01065000fd"
    assert xp.frame(xp.CMD_STAGE_INFO).hex() == "aa01065200ff"


def test_golden_stream():
    """8 实时读位移，周期 5 ms"""
    assert xp.stream_position(5, 0).hex() == "aa010808000005ae"


# ---------------- 数值编解码 ----------------

def test_value_matches_manual():
    """10.001 编出来必须是 00 0a 00 0a（整数部分 10、小数部分 10 个 1e-4）"""
    assert xp.encode_value(10.001) == bytes.fromhex("000a000a")
    assert xp.decode_value(bytes.fromhex("000a000a")) == 10.001


def test_value_negative_is_sign_magnitude():
    """负数靠最高位置 1，不是补码：-1.5 → 80 01 13 88"""
    assert xp.encode_value(-1.5) == bytes.fromhex("80011388")
    assert xp.decode_value(bytes.fromhex("80011388")) == -1.5


def test_value_roundtrip():
    for v in (0.0, 1e-4, -1e-4, 0.5, -12.3456, 100.0, -100.0, 32767.9999, -32767.9999):
        assert abs(xp.decode_value(xp.encode_value(v)) - v) < 1e-9, v


def test_value_rejects_out_of_range():
    for v in (32768.0, -32768.0, 1e9):
        try:
            xp.encode_value(v)
        except ValueError:
            continue
        raise AssertionError(f"{v} 应该被拒")


# ---------------- 解析器 ----------------

def test_parse_split_into_single_bytes():
    """半包：一帧拆成 11 次喂进去，只能在最后一字节才出来"""
    p = xp.Parser()
    raw = xp.frame(xp.CMD_READ_POSITION, bytes((0,)))
    for b in raw[:-1]:
        assert p.feed(bytes((b,))) == []
    got = p.feed(raw[-1:])
    assert len(got) == 1 and got[0].b3 == xp.CMD_READ_POSITION and got[0].raw == raw


def test_parse_two_frames_in_one_chunk():
    """粘包：一次喂两帧"""
    p = xp.Parser()
    a = xp.frame(xp.CMD_MODEL)
    b = xp.frame(xp.CMD_READ_UNIT)
    got = p.feed(a + b)
    assert [f.b3 for f in got] == [xp.CMD_MODEL, xp.CMD_READ_UNIT]
    assert p.dropped == 0 and p.bad_bcc == 0


def test_parse_skips_garbage():
    """前面有噪声字节要能对齐帧头"""
    p = xp.Parser()
    got = p.feed(b"\x00\x11\x22" + xp.frame(xp.CMD_HANDSHAKE))
    assert len(got) == 1 and got[0].b3 == xp.CMD_HANDSHAKE
    assert p.dropped == 3


def test_parse_handshake_reply():
    """77 的回包数据段是 b\"OK\"，整包 8 字节 —— 与指令表「返回整包长 8」对得上"""
    p = xp.Parser()
    got = p.feed(xp.frame(xp.CMD_HANDSHAKE, b"OK"))
    assert len(got) == 1 and got[0].data == b"OK" and len(got[0].raw) == 8


def test_parse_counts_bad_bcc():
    """校验错的帧不算数，且要计数"""
    p = xp.Parser()
    bad = bytearray(xp.frame(xp.CMD_MODEL))
    bad[-1] ^= 0xFF
    assert p.feed(bytes(bad)) == []
    assert p.bad_bcc == 1


def test_parse_resyncs_on_illegal_length():
    """包长字段被改坏 → 丢掉假帧头，后面的好帧照样能解出来"""
    p = xp.Parser()
    bogus = bytes((xp.HEADER, 0x01, 0x03))          # 包长 3 < MIN_FRAME
    good = xp.frame(xp.CMD_MODEL)
    got = p.feed(bogus + good)
    assert len(got) == 1 and got[0].b3 == xp.CMD_MODEL


def test_parse_reply_value():
    """下位机回包：11 字节，数据段 = 通道号 + 4 字节位移"""
    p = xp.Parser()
    reply = xp.frame(xp.CMD_READ_POSITION, bytes((0,)) + xp.encode_value(12.3456))
    got = p.feed(reply)
    assert len(got) == 1
    assert got[0].channel == 0
    assert abs(got[0].value - 12.3456) < 1e-9


def test_parse_short_reply_has_no_value():
    """7 字节回包（53/78）只有单位码/机型码，没有数值"""
    p = xp.Parser()
    got = p.feed(xp.frame(xp.CMD_READ_UNIT, bytes((4,))))
    assert len(got) == 1 and got[0].value is None and got[0].channel == 4


# ---------------- 直接跑 ----------------

def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"FAIL  {name}\n      {type(exc).__name__}: {exc}")
        else:
            print(f"ok    {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
