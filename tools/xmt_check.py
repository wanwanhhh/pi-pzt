"""芯明天 E53.D1S-H 上机自检：只读，绝不发运动指令。

回答这些还没验证的前提（实测与结论见 docs/xmt/设备认识账.xml）：

    阶段 1   设备认不认我们的帧     —— 波特率 × 地址 扫描 + 77 握手
    阶段 2   身份与行程             —— 47 地址 / 78 型号 / 53 单位 / 19 开闭环 / 27 35 行程
    阶段 2.5 静止读数噪声底          —— 两批各 20 次，取较小极差；顺带验读数是不是活的
    阶段 3   50 ms 帧间隔管不管主机   —— 连发两帧，看第二帧会不会被丢
    阶段 4   周期推送能跑到几毫秒      —— 50/20/10/5/2/1 ms，测实际到达间隔（**表征用**）
    阶段 5   流开着还能不能发别的命令   —— 流跑着的时候发 19
    阶段 6   设备自报能力             —— 80（含 256 条命令能力位图）/ 82

安全边界：本脚本只发白名单里的只读命令。不写目标、不切开闭环、写零更不行。
让它动是另一件事，见 tools/xmt_move.py（有 19=='C' 硬门）。

**单位尺度本脚本量不了**（要动才量得出），见 tools/xmt_scale.py：
读回 ×0.75 = µm，设点是 µm；行程 27/35 的单位与它同等存疑（认识账 A2/B1），只当参考区间。

用法：
    python tools/xmt_check.py                   # 自动找 VID_0483&PID_0002
    python tools/xmt_check.py --port COM7
    python tools/xmt_check.py --no-stream       # 跳过阶段 4/5
    python tools/xmt_check.py --stream-seconds 1
"""
from __future__ import annotations

import argparse
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import serial
from serial.tools import list_ports

from backend import xmt_protocol as xp

VID, PID = 0x0483, 0x0002
# 指令表 63 号支持的档位；设备**掉电保持**上次波特率，所以停在哪个都得能扫到
BAUD_CANDIDATES = (115200, 9600, 19200, 38400, 57600, 76800, 128000, 230400, 256000)

# 协议 §2.5「帧间隔时间：最少 50ms」。
# 但固定 50 ms 是 20 Hz 均匀网格：闭环若在 20 Hz 附近振荡，拍频极低，
# 连续 N 次读数会看起来纹丝不动 —— 判据的鲁棒性来自**间隔抖动**，不是采样率。
FRAME_GAP = 0.05
FRAME_JITTER = 0.015
READ_WAIT = 0.5

# 只读命令白名单：本脚本永远只发这些（send 的 allow 放行在**本文件里**一次都不许用）
SAFE = frozenset((
    xp.CMD_READ_ADDRESS, xp.CMD_HANDSHAKE, xp.CMD_MODEL, xp.CMD_READ_UNIT,
    xp.CMD_READ_LOOP_MODE, xp.CMD_READ_POSITION, xp.CMD_READ_POS_LIMIT_HIGH,
    xp.CMD_READ_POS_LIMIT_LOW, xp.CMD_STREAM_POSITION, xp.CMD_STOP_STREAM,
    xp.CMD_POWER_INFO, xp.CMD_STAGE_INFO,
))

# 能力位图里要盯的命令：文档说「没有」或「仅他型号有」的那几条全在里面
WATCHED = (0, 1, 5, 6, 7, 8, 11, 18, 19, 22, 23, 26, 27, 34, 35, 47, 53, 77, 78, 80, 82,
           108, 109, 110)

# 78 的机型码表（手册，止于 0x17 E80.D3S-O；E53.D1S-H 不在表里）
MODEL_NAMES = {
    0x00: "E70.S3", 0x01: "E18", 0x02: "E53", 0x03: "E18 24位", 0x04: "E51.D12S",
    0x05: "E72", 0x06: "E63", 0x07: "长光机定制709", 0x08: "E70 4路",
    0x09: "E70-D3S-H5", 0x0A: "E80-D3S-k1", 0x0B: "E70-D3S-K1",
}


def find_port(explicit: str | None = None) -> str | None:
    """按 VID:PID 找目标串口；显式指定了端口就直接用。会动的脚本也用它。"""
    if explicit:
        return explicit
    for p in list_ports.comports():
        if (p.vid, p.pid) == (VID, PID):
            return p.device
    return None


class Link:
    """串口 + 解析器。收发都从这里过，方便统计丢包与校验失败。"""

    def __init__(self, port: str) -> None:
        self.ser = serial.Serial(port, BAUD_CANDIDATES[0], timeout=0)
        self.parser = xp.Parser()
        self.baud = BAUD_CANDIDATES[0]
        self.addr = 1

    def close(self) -> None:
        self.ser.close()

    def set_baud(self, baud: int) -> None:
        self.ser.baudrate = baud
        self.baud = baud
        self.drain()

    def drain(self) -> None:
        """丢掉残留，让每轮请求从干净状态开始。"""
        self.ser.reset_input_buffer()
        self.parser = xp.Parser()

    def cmd(self, b3: int, data: bytes = b"") -> bytes:
        return xp.frame(b3, data, addr=self.addr)

    def ping(self) -> None:
        """静默之后补一次心跳，免得「上一条未返回后续不执行」把自己卡住。"""
        self.ask(self.cmd(xp.CMD_HANDSHAKE), xp.CMD_HANDSHAKE, wait=0.3)

    def send(self, raw: bytes, allow: frozenset = frozenset()) -> None:
        """默认只放只读白名单；写命令必须由调用方用 allow 显式点名。

        allow 是给 tools/xmt_move.py、tools/xmt_scale.py 那两个会动的兄弟脚本用的，
        它们的**调用点**写着 `allow=ALLOW`，一眼能看出这个脚本要动台子。
        本文件（只读自检）一处都不许用 —— xmt_check_safety 测试会遍历所有 .send
        调用，带第二个位置参数或 allow 关键字的一律判失败。

        用 raise 不用 assert：assert 在 python -O 下会整个消失。
        """
        if raw[3] not in SAFE and raw[3] not in allow:
            raise RuntimeError(f"不在白名单里，本脚本不允许发: {raw.hex()}")
        self.ser.write(raw)

    def gap(self) -> None:
        """帧间隔：协议下限 50 ms + 抖动，破掉均匀网格的相位锁定。"""
        time.sleep(FRAME_GAP + random.uniform(0.0, FRAME_JITTER))

    def wait(self, seconds: float = READ_WAIT) -> list[xp.Frame]:
        out: list[xp.Frame] = []
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            n = self.ser.in_waiting
            if n:
                out += self.parser.feed(self.ser.read(n))
            else:
                time.sleep(min(left, 0.0005))
        return out

    def poll(self, seconds: float) -> list[tuple[float, xp.Frame]]:
        """同 wait，但给每帧打到达时刻，用来测推送周期。"""
        out: list[tuple[float, xp.Frame]] = []
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            n = self.ser.in_waiting
            if n:
                now = time.monotonic()
                out += [(now, f) for f in self.parser.feed(self.ser.read(n))]
            else:
                time.sleep(min(left, 0.0005))
        return out

    def ask(self, raw: bytes, want_b3: int, wait: float = READ_WAIT) -> xp.Frame | None:
        """发一帧、等它对应的回包（一到就返回），之后留一个带抖动的帧间隔。"""
        self.drain()
        self.send(raw)
        end = time.monotonic() + wait
        got: list[xp.Frame] = []
        found: xp.Frame | None = None
        while found is None:
            left = end - time.monotonic()
            if left <= 0:
                break
            for f in self.wait(min(left, 0.05)):
                got.append(f)
                if f.b3 == want_b3:
                    found = f
                    break
        self.gap()
        if found is None:
            print(f"    ! 期待 B3={want_b3:#04x}，实际收到 {[hex(f.b3) for f in got] or '空'}")
        return found


# ---------------- 阶段 ----------------

def stage_port(args: argparse.Namespace) -> str | None:
    print("== 阶段 0：找端口 ==")
    for p in list_ports.comports():
        hit = (p.vid, p.pid) == (VID, PID)
        print(f"  {p.device:<8} {p.vid and format(p.vid, '04X')}:{p.pid and format(p.pid, '04X')}"
              f"  {p.description}{'   <-- 目标' if hit else ''}")
    port = find_port(args.port)
    if args.port:
        print(f"  用 --port 指定的 {args.port}")
    elif port is None:
        print(f"  没找到 VID:PID={VID:04X}:{PID:04X}")
    return port


def stage_handshake(port: str) -> Link | None:
    """波特率 × 地址 扫描。

    握手失败**不等于**协议层有 bug —— 下位机关机后保留上次波特率
    （上位机软件使用说明书），而地址也可能不是 1（指令表原文：「地址码一般从 1 开始」）。
    所以先把这两种可能排掉，再谈组帧。
    """
    print("\n== 阶段 1：握手（77）—— 波特率 × 地址 扫描 ==")
    link = Link(port)
    for baud in BAUD_CANDIDATES:
        link.set_baud(baud)
        for addr in (1, 0):
            link.addr = addr
            f = link.ask(link.cmd(xp.CMD_HANDSHAKE), xp.CMD_HANDSHAKE, wait=0.4)
            where = "广播" if addr == 0 else f"地址{addr}"
            if f is None:
                print(f"  {baud:>6} 8N1 / {where} → 无回音")
                continue
            ok = f.data == b"OK"
            print(f"  {baud:>6} 8N1 / {where} → {f.raw.hex()} 数据段={f.data!r}"
                  f"  {'通了' if ok else '不是 OK，协议对不上'}")
            if ok:
                link.addr = addr
                return link
    print("  全部波特率 × 地址组合都没有回音 —— 先别怀疑组帧：")
    print("    ① 厂家上位机是不是还占着串口    ② 设备是不是没上电")
    print("    ③ 用厂家上位机的「USB 设置串口波特率」把它设回 115200 再试")
    link.close()
    return None


def read_address(link: Link) -> int | None:
    """47 读地址。指令表写明下发帧的地址字节必须是 0（广播）。

    读到之后**必须采纳**：否则整趟都跑在广播上，下一趟的写脚本会继承成广播写。
    """
    saved = link.addr
    link.addr = 0
    f = link.ask(link.cmd(xp.CMD_READ_ADDRESS), xp.CMD_READ_ADDRESS)
    addr = f.data[0] if f is not None and f.data else None
    if addr is None:
        link.addr = saved
        return None
    link.addr = addr
    if link.ask(link.cmd(xp.CMD_HANDSHAKE), xp.CMD_HANDSHAKE, wait=0.4) is None:
        print(f"    ! 用地址 {addr} 单播复验失败，退回 {saved}")
        link.addr = saved
        return None
    return addr


def stage_identity(link: Link) -> dict:
    print("\n== 阶段 2：身份与行程 ==")
    ident: dict = {}

    addr = read_address(link)
    if addr is not None:
        print(f"  47 本机地址 = {addr}（当前用 {link.addr} 寻址）")
        ident["addr"] = addr

    f = link.ask(link.cmd(xp.CMD_MODEL), xp.CMD_MODEL)
    if f is not None and f.data:
        code = f.data[0]
        print(f"  78 型号码 = {code:#04x}  ({MODEL_NAMES.get(code, '表里没有，查 docs/xmt/protocol/')})")
        ident["model"] = code

    f = link.ask(link.cmd(xp.CMD_READ_UNIT), xp.CMD_READ_UNIT)
    if f is not None and f.data:
        code = f.data[0]
        warn = "" if code in (1, 4, 5) else "   <-- 不在白名单 {1,4,5} 里，折算规则要重新确认"
        print(f"  53 单位码 = {code}  ({xp.UNITS.get(code, '未知')}){warn}")
        ident["unit"] = code

    f = link.ask(link.cmd(xp.CMD_READ_LOOP_MODE, bytes((0,))), xp.CMD_READ_LOOP_MODE)
    if f is not None and len(f.data) >= 2:
        mode = f.data[1:2]
        print(f"  19 开闭环 = {mode!r}  (b'O' 开环 / b'C' 闭环)"
              + ("" if mode == b"C" else "   <-- 开环！此时 B3=1 的数值是伏特，绝不能发设点"))
        ident["loop"] = mode

    hi = link.ask(link.cmd(xp.CMD_READ_POS_LIMIT_HIGH, bytes((0,))), xp.CMD_READ_POS_LIMIT_HIGH)
    lo = link.ask(link.cmd(xp.CMD_READ_POS_LIMIT_LOW, bytes((0,))), xp.CMD_READ_POS_LIMIT_LOW)
    hi_v = hi.value if hi is not None else None
    lo_v = lo.value if lo is not None else None
    if hi_v is None or lo_v is None:
        print(f"  27/35 行程读不出来（高限 {hi_v}，低限 {lo_v}）"
              "—— 回包数据段不足 5 字节，或设备没回")
    else:
        print(f"  27 高限 = {hi_v}    35 低限 = {lo_v}    宽 = {hi_v - lo_v}")
        ident["high"], ident["low"] = hi_v, lo_v
    return ident


def stage_idle_reads(link: Link, n: int = 20, gap_s: float = 2.0) -> dict:
    """台子不动，连读两批各 n 次。

    - 极差 = 读数噪声底（取两批较小的那个）。**口径**：极差是**读回单位**，
      ×0.75 才是 µm（见 docs/xmt/设备认识账.xml E11）。它现在只用来解释「读回确认」
      的容差够不够宽 —— 生产端已经不判稳了（见 xmt_stage.wait_on_target）。
    - 是否逐次完全相同：闭环反馈是实时传感器，静止时也该有末位抖动；
      若两批都一字不差，要怀疑读回来的是目标寄存器而不是传感器。
      注意这个启发式**只是怀疑**：单位码粗（如 4=mm，1e-4 mm = 0.1 µm）时，
      真传感器的噪声也可能落在量化台阶之下。
    """
    print(f"\n== 阶段 2.5：静止读数（台子不动，连读 {n} 次 × 2 批）==")
    batches: list[list[float]] = []
    for k in range(2):
        vals: list[float] = []
        for _ in range(n):
            f = link.ask(link.cmd(xp.CMD_READ_POSITION, bytes((0,))), xp.CMD_READ_POSITION,
                         wait=0.3)
            if f is None or f.value is None:
                print("  读位移失败，跳过这一段")
                return {}
            vals.append(f.value)
        batches.append(vals)
        if k == 0:
            time.sleep(gap_s)

    spreads = [max(v) - min(v) for v in batches]
    for i, (v, s) in enumerate(zip(batches, spreads), 1):
        print(f"  第 {i} 批 首值 {v[0]:g}  末值 {v[-1]:g}  极差 {s:g}"
              f"（读回单位；≈ {s * 0.75:g} µm）")
    identical = all(len(set(v)) == 1 for v in batches)
    print("  " + ("两批都逐次完全相同 —— 可疑：可能读的是目标值而不是传感器"
                  if identical else "有末位抖动 —— 是活的传感器读数，正常"))
    print(f"  → 噪声底 = {min(spreads):g} 读回单位"
          f"（≈ {min(spreads) * 0.75:g} µm；两批里最乐观的那个，是**下限**）")
    print("     必须长时间静止后测；刚跑过运动时这个数偏大。")
    print("     生产端不判稳：移动后等满界面上的稳定延时、再读一次回（容差 XMT_ARRIVAL_TOL_UM）。")
    print("     这条噪声底用来判断容差够不够宽、等待时长会不会太短（走完 + 整定的时间）。")
    return {"spread": min(spreads), "identical": identical}


def stage_frame_gap(link: Link) -> dict:
    print("\n== 阶段 3：50 ms 帧间隔管不管主机 ==")
    out: dict = {}

    # A：上一条已经回来了，只隔 10 ms 就发下一条 —— 单独测帧间隔。
    # 两条命令不同（78 然后 53），才分得清是哪一条没回。
    link.drain()
    link.send(link.cmd(xp.CMD_MODEL))
    if not [f for f in link.wait(0.3) if f.b3 == xp.CMD_MODEL]:
        print("  A 第一帧（78）就没有回包 —— 链路本身有问题，这一阶段作废")
        return {"usable": False}
    time.sleep(0.01)
    link.send(link.cmd(xp.CMD_READ_UNIT))
    n_a = len([f for f in link.wait(0.4) if f.b3 == xp.CMD_READ_UNIT])
    link.gap()
    print(f"  A 上一条已返回、隔 10 ms 再发 → 第二条（53）收到 {n_a} 条"
          "（1 = 短于 50 ms 也照收；0 = 被丢弃）")
    out["gap"] = n_a

    # B：两帧背靠背 —— 测「上一条未返回前，后续指令不执行」
    link.drain()
    link.send(link.cmd(xp.CMD_MODEL))
    link.send(link.cmd(xp.CMD_READ_UNIT))
    got = link.wait(0.5)
    seen = sorted({f.b3 for f in got})
    n_b = len([f for f in got if f.b3 == xp.CMD_READ_UNIT])
    link.gap()
    print(f"  B 两帧背靠背（78 然后 53）→ 收到 {[hex(b) for b in seen]}"
          "（两条都有 = 排队；只有 78 = 第二条被丢弃）")
    out["back2back"] = n_b
    out["usable"] = True
    return out


def stage_stream(link: Link, seconds: float) -> dict:
    """**表征用**，不是生产需要。

    生产端慢轮询就够（AGENTS.md 通用需求）。这里测的是「怎么停的」——
    超调、振铃、蠕变，50 ms 采样看不见。
    """
    print(f"\n== 阶段 4：周期推送（表征用，每档采 {seconds} s）==")
    out: dict = {}
    for period in (50, 20, 10, 5, 2, 1):
        link.drain()
        link.send(link.cmd(xp.CMD_STREAM_POSITION, bytes((0, period))))
        samples = link.poll(seconds)
        link.send(link.cmd(xp.CMD_STOP_STREAM))
        tail = link.poll(0.3)
        dropped, bad = link.parser.dropped, link.parser.bad_bcc
        link.drain()
        link.gap()

        sel = [t for t, f in samples if f.b3 == xp.CMD_STREAM_POSITION]
        if not sel:
            seen = sorted({hex(f.b3) for _, f in samples})
            print(f"  周期 {period:>3} ms → 没有位移回包（收到 {seen or '空'}），后续档位不再试")
            out[period] = None
            break
        gaps = [(b - a) * 1000 for a, b in zip(sel, sel[1:])]
        note = f"  [丢 {dropped} 字节 / 校验错 {bad} 帧]" if (dropped or bad) else ""
        if len(sel) > 1:
            print(f"  周期 {period:>3} ms → {len(sel):>4} 包，实测间隔"
                  f" 中位 {statistics.median(gaps):6.2f}  最小 {min(gaps):6.2f}"
                  f"  最大 {max(gaps):6.2f} ms{note}")
        else:
            print(f"  周期 {period:>3} ms → 只收到 1 包，无法测周期{note}")
        # 1 ms 档满流下 0.3 s 该收到约 300 包，停流后最多只剩在途的 1 包。
        # 阈值取 >1，不是"收了就报警"：否则每档都会误报停流没生效。
        if len(tail) > 1:
            print(f"      ! 发 11 之后又收到 {len(tail)} 包 —— 停流没生效")
        elif len(tail) == 1:
            print("      · 发 11 之后还有 1 包，是在途的那一包，正常")
        out[period] = {"n": len(sel),
                       "median": statistics.median(gaps) if len(gaps) > 1 else None}
    return out


def stage_cmd_during_stream(link: Link, seconds: float) -> bool:
    print("\n== 阶段 5：流开着还能不能发别的命令 ==")
    link.drain()
    link.send(link.cmd(xp.CMD_STREAM_POSITION, bytes((0, 20))))
    link.poll(0.5)                                      # 先让流跑起来
    link.send(link.cmd(xp.CMD_READ_LOOP_MODE, bytes((0,))))   # 流运行中插一条只读命令
    samples = link.poll(seconds)
    link.send(link.cmd(xp.CMD_STOP_STREAM))
    link.poll(0.3)
    link.drain()
    link.gap()

    stream_n = sum(1 for _, f in samples if f.b3 == xp.CMD_STREAM_POSITION)
    other = [f.b3 for _, f in samples if f.b3 != xp.CMD_STREAM_POSITION]
    answered = xp.CMD_READ_LOOP_MODE in other
    print(f"  流中收到 {stream_n} 包位移，另有非流回包 {[hex(b) for b in other] or '无'}")
    print("  → 流运行期间可以下发并执行其它命令，遥测可以搭这趟顺风车" if answered else
          "  → 流运行期间那条命令没得到回包（扫描中改目标存疑，也说明遥测要单独排队）")
    return answered


def stage_capabilities(link: Link) -> dict:
    """让设备自报能力，别继续相信文档的否定式断言。"""
    print("\n== 阶段 6：设备自报能力（80 / 82）==")
    out: dict = {}
    link.ping()                                  # 先确认没被「上一条未返回」卡住

    f = link.ask(link.cmd(xp.CMD_POWER_INFO), xp.CMD_POWER_INFO, wait=1.0)
    if f is None:
        print("  80 电源信息没有回包")
    elif len(f.data) < 51:
        print(f"  80 只回了 {len(f.raw)} 字节（数据段 {len(f.data)}），短于预期的 61/55")
    else:
        d = f.data
        print(f"  80 回包 {len(f.raw)} 字节")
        for i, name in ((0, "CPU型号"), (1, "通道数"), (7, "是否恒压"),
                        (15, "传感器类型"), (16, "带宽(x1000Hz)"),
                        (17, "DA分辨率"), (18, "AD分辨率")):
            print(f"      [{5 + i}] {name} = {d[i]}")
        bm = d[19:51]
        lsb = [c for c in WATCHED if (bm[c // 8] >> (c % 8)) & 1]
        msb = [c for c in WATCHED if (bm[c // 8] >> (7 - c % 8)) & 1]
        print(f"      [24..55] 命令能力位图 {bm.hex()}")
        print(f"      好使（LSB 序）: {lsb}")
        print(f"      好使（MSB 序）: {msb}")
        if lsb != msb:
            print(f"      两种位序不一致，需人工核对，差异 {sorted(set(lsb) ^ set(msb))}")
        else:
            nonzero = [i for i, b in enumerate(bm) if b != 0xFF]
            print(f"      两种位序结论相同（这批数据分不出来）；非 0xFF 的字节下标 {nonzero}"
                  f" → 覆盖命令 {[i * 8 for i in nonzero]} 起")
        out["bitmap"], out["lsb"], out["msb"] = bm.hex(), lsb, msb

    f = link.ask(link.cmd(xp.CMD_STAGE_INFO), xp.CMD_STAGE_INFO)
    if f is not None:
        print(f"  82 台子信息 数据段 {f.data.hex()}（[10]通道数 [11]类型 [12]恒压 [13]R/C/L）")
        out["stage_info"] = f.data.hex()
    return out


def summary(ident: dict, idle: dict, gap: dict, streams: dict, during: bool | None,
            caps: dict) -> None:
    print("\n== 结论 ==")
    if ident.get("addr") is not None:
        print(f"  本机地址    : {ident['addr']}")
    if ident.get("model") is not None:
        print(f"  型号码      : {ident['model']:#04x}")
    if ident.get("unit") is not None:
        print(f"  单位码      : {ident['unit']} ({xp.UNITS.get(ident['unit'], '未知')})")
    if "high" in ident:
        print(f"  行程        : {ident['low']} ~ {ident['high']}"
              f"（宽 {ident['high'] - ident['low']}）"
              "   <-- 已查实：控制器没有标定数据，这是固件默认值，只能当参考区间；"
              "可用区间见 docs/xmt/设备认识账.xml A3")

    loop = ident.get("loop")
    if loop is None:
        print("  开闭环      : 读不到   <-- 任何写目标动作的前置硬门，先把它读到")
    elif loop == b"C":
        print("  开闭环      : 闭环，可以发设点")
    else:
        print(f"  开闭环      : {loop!r} 开环！B3=1 的数值是伏特不是位移，绝不能发设点")

    if idle:
        print(f"  读数噪声底  : {idle['spread']:g}"
              + ("   <-- 两批都一字不差，怀疑读的是目标值" if idle["identical"] else ""))
    if gap.get("gap") is not None:
        print(f"  50 ms 间隔  : 隔 10 ms 再发第二条收到 {gap['gap']} 条；"
              f"背靠背的第二条收到 {gap.get('back2back')} 条"
              f"（{gap['gap']} = 短间隔不被丢，{gap.get('back2back')} = 会排队）")
    elif gap:
        print("  50 ms 间隔  : 链路本身没通，这一项作废")
    ok = [p for p, v in streams.items() if v]
    if streams:
        print(f"  周期推送    : 能跑的档位 {sorted(ok)}"
              "（表征用；生产端慢轮询就够，不必依赖它）")
    if during is not None:
        print(f"  流中发命令  : {'可以' if during else '不行'}")
    if caps.get("lsb") is not None:
        print(f"  命令能力位图: LSB 序好使 {caps['lsb']}")
        print("                <-- 拿它去核对 AGENTS.md「没有到位查询/停止/关伺服」三条断言")


def main() -> int:
    # Windows 下输出被重定向到文件时 Python 默认用 GBK，日志会变成非 UTF-8 而没法直接贴。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="芯明天 E53.D1S-H 只读上机自检")
    ap.add_argument("--port", help="串口名，默认自动找 VID_0483&PID_0002")
    ap.add_argument("--no-stream", action="store_true", help="跳过阶段 4/5")
    ap.add_argument("--stream-seconds", type=float, default=1.5, help="每个周期档位的采样时长")
    args = ap.parse_args()

    port = stage_port(args)
    if not port:
        return 2
    try:
        link = stage_handshake(port)
    except serial.SerialException as exc:
        print(f"  打开 {port} 失败：{exc}")
        return 2
    if link is None:
        return 2

    print(f"  已打开 {port} @ {link.baud} 8N1，地址 {link.addr}"
          " —— 只读模式，不会让台子动")
    try:
        ident = stage_identity(link)
        if not ident:
            print("  阶段 2 全空，握手可能是假的，停在这里")
            return 1
        idle = stage_idle_reads(link)
        gap = stage_frame_gap(link)
        streams: dict = {}
        during: bool | None = None
        if not args.no_stream:
            streams = stage_stream(link, args.stream_seconds)
            during = stage_cmd_during_stream(link, args.stream_seconds)
        caps = stage_capabilities(link)
        summary(ident, idle, gap, streams, during, caps)
    except KeyboardInterrupt:
        print("\n  被中断，正在收尾")
    finally:
        link.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
