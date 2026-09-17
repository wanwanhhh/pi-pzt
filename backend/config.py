"""集中配置。所有可调数字都在这里。"""
from __future__ import annotations

import os
from pathlib import Path

# 项目根目录（backend/ 的上一级）：data/、frontend/ 都相对它定位
BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------- 设备 ----------------
# 启用哪一台：pi = PI E-709，xmt = 芯明天 E53.D1S-H。同一时刻只激活一台。
# 具体类由 stage_api.create_stage() 延迟导入，不会被没选中的那台拖进依赖。
DEVICE = os.getenv("PI_DEVICE", "pi").lower()
DEVICE_NAME = os.getenv("PI_DEVNAME", "E-709")
# USB 通道的序列号过滤（ConnectUSB 的参数），留空则自动枚举 USB 上唯一的控制器。
# 只在 LINK=usb 时生效；串口通道按 /dev/serial/by-id 定位，不使用本项。
DEVICE_SERIAL = os.getenv("PI_SERIAL", "")
AXIS = os.getenv("PI_AXIS", "X")

# 连接方式：auto | usb | serial
#   auto   Linux 走串口，Windows 走 USB（见 AGENTS.md 技术栈）
#   usb    强制 GCS DLL 通道（Windows 常规路径，需 PI Software Suite）
#   serial 强制 FTDI 虚拟串口（Linux 常规路径，纯 Python，不需要 .so）
LINK = os.getenv("PI_LINK", "auto")
# 串口设备路径。留空则自动找 /dev/serial/by-id/usb-PI_*
# （不写死 ttyUSB0：换 USB 口或插别的串口设备时编号会变）
SERIAL_PORT = os.getenv("PI_SERIAL_PORT", "")
SERIAL_BAUD = int(os.getenv("PI_SERIAL_BAUD", "115200"))

# ---------------- 芯明天 E53.D1S-H（PI_DEVICE=xmt，仅 Windows） ----------------
# USB CDC 虚拟串口，系统自带 usbser.sys，不需要厂家驱动。按 VID:PID 自动找，
# 不写死 COM7：换 USB 口编号会变。
XMT_USB_VID = 0x0483
XMT_USB_PID = 0x0002
XMT_PORT = os.getenv("PI_XMT_PORT", "")          # 留空则按 VID:PID 找
XMT_BAUD = int(os.getenv("PI_XMT_BAUD", "115200"))
XMT_ADDRESS = int(os.getenv("PI_XMT_ADDR", "1"))

# **单位换算，实测出来的，别改**：设点 1 与行程 27/35 是 µm，
# 读回 6/8 是 4/3 µm（×0.75 才是 µm）。53 只回「位移」，手册全书没写这个系数。
# 读回值不折算就写回设点，台子会跑偏 4/3 倍（实测 108.66 → 144.95 µm）。
XMT_READBACK_TO_UM = 0.75
# 行程读数超出这个范围就认为读错了（设备没标定 / 串了口），拒绝连接
XMT_MAX_TRAVEL_UM = 1000.0

XMT_READ_TIMEOUT = 0.3      # 单条读命令等回包上限（s）
XMT_CMD_TIMEOUT = 5.0       # 单个 owner 任务上限（s）

# 停稳判据（设备没有到位信号，只能软件判；见 xmt_stage.SettleJudge）。
# ε 与窗口长度一起决定判据：实测单次噪声 σ 0.040 µm，ε 取 2.5 倍。
# 采样间隔基数。实际间隔 = max(这个值, 帧间隔 50 ms) + 抖动 —— 调到 50 以下不会更快。
XMT_POLL_MS = 50
XMT_POLL_JITTER_MS = 15     # 抖动上限：固定网格会与闭环振荡拍频，看起来纹丝不动
XMT_SETTLE_WINDOW = 5       # 一窗几个样本（≈250~325 ms）
XMT_SETTLE_WINDOWS = 2      # 连续几窗均值差都在 ε 内才算停稳
XMT_SETTLE_EPS_UM = 0.1     # 窗口均值之间"算没动"的门限（µm）
# 读回校验的容差：偏离目标超过它就判该点无效（设点无应答，丢帧是静默的）。
# **必须远小于扫描步距**：丢帧的表现就是"整整差一个步距"，步距 ≤ 容差时这个校验
# 形同虚设。实测定位误差 ±0.045 µm、真机 1 µm 步距扫描里最坏一次差 0.12 µm，
# 所以取 0.2：既留了余量，又只要求步距 > 0.2 µm。
XMT_ARRIVAL_TOL_UM = 0.2

# 读不到设备限位时的兜底值（正常情况用不到，仅防御）
FALLBACK_TRAVEL_MIN = 0.0
FALLBACK_TRAVEL_MAX = 100.0

# 软件限位安全边距（µm）：指令会被夹到 [min+margin, max-margin]。
# 取 0 = 直接用设备行程端点（TMN?/TMX?）。闭环压电台的行程端点是标定范围内的
# 工作点，不是机械挡块，凭空调小只会让全行程扫描（0→100）被拒。要留余量改这里。
SOFT_LIMIT_MARGIN = 0.0
# 扫描首点的预逼近退让量（µm）：先退到起点外侧再逼近，
# 让首点与后续点从同一侧过来（否则首帧相对其余点错开约 0.1 µm）
APPROACH_OFFSET_UM = 2.0
# 允许设定的最大速度（µm/s）
MAX_VELOCITY = 10000.0

# ---------------- 遥测 ----------------
# 设备查询很贵（实测约 32 ms/条，DLL 通道上限约 31 条/秒），遥测与扫描要抢这个带宽。
# 扫描进行中降到 TELEMETRY_HZ_SCAN，把带宽让给扫描本身。
TELEMETRY_HZ = 10.0        # 空闲时的状态推送频率
TELEMETRY_HZ_SCAN = 2.0    # 扫描进行中的推送频率
SLOW_QUERY_EVERY = 10      # 每 N 次快查询做一次慢查询（PI 是 SVO?/ERR?/OVF?/MOV?/VEL?；
                           # XMT 只用它刷 19 开闭环）

# ---------------- 扫描 ----------------
DEFAULT_SETTLE_MS = 100    # ONT? 置位后的额外稳定延时
ON_TARGET_TIMEOUT_S = 10.0 # 单点等待到位上限
SETTLE_MS_RANGE = (0, 5000)

# ---------------- CCD ----------------
# dummy = 生成灰度占位图（打通"每点到位->采图->写回元数据"链路）；接真相机时换实现
CCD_BACKEND = os.getenv("PI_CCD", "dummy")

# ---------------- 存储 ----------------
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "scans.db"
IMAGE_DIR = DATA_DIR / "images"

# ---------------- 服务 ----------------
STATIC_DIR = BASE_DIR / "frontend"
HOST = "127.0.0.1"
PORT = int(os.getenv("PI_PORT", "8000"))
# 心跳只用于界面显示"前端在线"，不用于停止扫描（扫描独立于浏览器）
HEARTBEAT_TIMEOUT_S = 3.0
