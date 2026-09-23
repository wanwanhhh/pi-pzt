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

# **设备自报的行程不是可用行程。** 设备里没有任何标定数据（45/82/59 全空），
# 27/35 只是固件默认值，而且永不生效 —— 先到的是电压轨：设点 ≈159.4 µm 处驱动量
# 撞上它自己的上限 200，162 µm 就翻进「负位置 + 负驱动」，要下发 100 µm 才拉得回来。
# 下界同理：≲3 µm 报的不是测量值（σ<0.2 nm、格子 1e-4，都比一个 AD 码 3.225 nm 小），
# 2.4~2.8 µm 是过渡带。实测可用区间 ≈ 3.1~159 µm，这里取 5~150 留余量。
# 依据见 docs/xmt/设备认识账.xml 的 A3 / A5。
XMT_USABLE_MIN_UM = 5.0
XMT_USABLE_MAX_UM = 150.0

# 「为什么没到位」的诊断：读回确认没落在目标附近时，用两次确认到位之间的差分把原因说清楚 ——
# 读数几乎没动 → 像设点丢帧；读数动了但比值不是 4/3 → 像折算系数变了（被重新标定过）。
# **只诊断，不拦截**：兜底的是到达容差（系数一变，读数再也落不到目标附近）；
# 拦截会多一个误判源 —— 丢帧时台子停在上一目标，拿它跟新目标算比值必然算错，
# 这个误判在本机上闩死过两次（要重启进程）。细节见 docs/xmt/设备认识账.xml P0.5。
XMT_SCALE_CHECK_MIN_UM = 20.0     # 两次确认到位之间的位移小于它就不核（比值不够准）
XMT_SCALE_TOL_FRACTION = 0.02     # 比值允许偏离 4/3 的比例
XMT_SCALE_NO_MOTION = 0.05        # 读数变化小于步子的这个比例 → 判设点丢帧，不判标定

XMT_READ_TIMEOUT = 0.3      # 单条读命令等回包上限（s）
XMT_CMD_TIMEOUT = 5.0       # 单个 owner 任务上限（s）

# 到位判据：**不判稳**（2026-09 起）。设备没有到位信号，做法是「等满界面上设的稳定延时，
# 再读一次回」—— 等多久由用户负责，旋钮动过要重设（见 docs/xmt/设备认识账.xml E12/E7）。
# 单次读数噪声 σ 28~47 nm（位置相关，口径见设备认识账 E11）只用来判断这条容差够不够宽。
# 读回确认的容差：偏离目标超过它就算没到位（设点无应答，丢帧是静默的）。
# **必须远小于扫描步距**：丢帧的表现就是"整整差一个步距"，步距 ≤ 容差时这个校验
# 形同虚设。实测偏差 = 单次读数噪声 σ 0.03 µm + 台子自己的稳定偏置（设点 22.0 µm 时
# +0.073 µm，220 s 不收敛；145 µm 段变号成 −0.04），最坏 0.12 µm，所以取 0.2：
# 既留了余量，又只要求步距 > 0.2 µm。偏置随区域变号，不能拿常数补掉。
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

# ---------------- 位置曲线（观察稳定性） ----------------
# 曲线取自遥测轮询的环形缓冲，**不额外轮询设备**：设备带宽是稀缺资源（见上面遥测一节）。
# 缓冲时长要盖住「一次记录 + 迟到的最后一次取数」：浏览器把后台标签的定时器压到约
# 1 次/分钟，窗口两端都是绝对时刻，所以迟到也能取回完整的那一段 —— 但样本得还在缓冲里。
# 曲线密度 = 遥测频率：XMT 上实测约 9.2 Hz（间隔 108 ms），扫描中降到 2 Hz，点数会明显变少。
TRACE_BUFFER_S = 120.0
TRACE_MIN_S = 1.0
TRACE_MAX_S = 60.0

# ---------------- 扫描 ----------------
# 单点偏差超过「步距 × 这个系数」就判该点无效：不采图、不入库、中止扫描。
# 取 0.5 的理由：设点丢帧的表现是**整整差一个步距**，永远大于半个步距，所以这条
# 在步距多小的时候都成立；而它只收半个步距，不会拿正常的定位误差误伤。
# 设备层的到达容差是绝对的（XMT 0.2 µm），步距 ≤ 容差时那道校验会失效 —— 这条补盲区。
SCAN_ARRIVAL_FRACTION = 0.5

# 界面上那个「稳定延时」的默认值，**每台设备不一样**（caps.default_settle_ms）：
#   PI  = 到位信号置位后的额外延时：控制器说到了就够了，100 ms 是认识账里记的默认。
#   XMT = 移动后唯一的等待：要覆盖「走完一步 + 整定」（刚走完 2 s 内还蠕变 +0.025 µm），
#         所以给 300 ms。设短了不会报错，但会采到还在动的点。
DEFAULT_SETTLE_MS = 100
XMT_DEFAULT_SETTLE_MS = 300
ON_TARGET_TIMEOUT_S = 10.0 # 单点等待到位上限
SETTLE_MS_RANGE = (0, 5000)

# ---------------- CCD ----------------
# null  = 不采图        dummy = 生成灰度占位图（打通链路用）
# thorlabs = 索雷博 CS165MU（Zelux），仅 Windows，见 backend/thorlabs_ccd.py
CCD_BACKEND = os.getenv("PI_CCD", "dummy")

# --- 索雷博 CS165MU ---
# 原生 DLL 目录（SDK 包里 dlls\64_lib）。设备层会把 19 个厂商 DLL 按全路径预加载，
# 所以**不要求**先把它塞进 PATH；run_camera.bat 里设 PATH 是给工具脚本兜底。
# 细节与踩坑见 docs/thorlabs/设备认识账.xml 的 A2 / E1。
# 没设 TL_SDK_DLLS 时按本机的 SDK 解压位置兜底：跟 run_camera.bat 的 %USERPROFILE% 同一个意思，
# 不写死用户名。真找不到时设备层会明确报"缺哪个 DLL"，不会静默失败。
TL_DLL_DIR = os.getenv("TL_SDK_DLLS") or str(
    Path(os.getenv("USERPROFILE") or Path.home())
    / "Desktop/pzt/Scientific_Camera_Interfaces/Scientific Camera Interfaces"
      "/SDK/Python Toolkit/dlls/64_lib"
)
# **增益和曝光都是掉电保持的**：进程退出后相机自己记着上次的值。
# 所以每次连接都必须显式写一遍并读回，绝不能假设默认是 0 ——
# 实测踩过：上一个脚本把增益拉到 480 档没还原，后面几轮全被误判成"光太强、要加衰减片"。
# 增益放大信号的同时放大噪声，信噪比不会变好，所以本机恒为 0，亮度只用曝光调。
TL_GAIN = int(os.getenv("PI_CCD_GAIN", "0"))
# 曝光：**预览与采图共用一个值**（分开的话"预览看着挺好"和"存下来的"就是两张亮度不同的图）。
# 界面上可以实时调；每一帧保存时把**相机读回的实际曝光**写进元数据 —— 图像自证用了哪次参数。
TL_EXPOSURE_US = int(os.getenv("PI_CCD_EXPOSURE_US", "12000"))
TL_FULL_ROI = (0, 0, 1440, 1080)      # 原生全幅：保存一律用它，不裁剪
# 预览 ROI：默认**原生全幅**，跟保存用的一样 —— 预览就该看到整个视场。
# 早先为了跑得快设成左上角 520x520，结果预览只显示全幅的 18%，容易误以为"视野就这一块"。
# 相机端裁剪确实能提帧率（实测 528x528 上限约 70 fps、全幅约 35 fps），
# 真嫌慢再改这里（ROI 会被硬件对齐改写，所以一律按读回值记账）。
TL_PREVIEW_ROI = (0, 0, 1440, 1080)
TL_PREVIEW_FPS = float(os.getenv("PI_CCD_PREVIEW_FPS", "15"))
TL_JPEG_QUALITY = int(os.getenv("PI_CCD_JPEG_QUALITY", "80"))
# 满量程：**实测值**（认识账 A3）——SDK 交出来的最大值是 1022，不是 4095 也不是 65535。
# 饱和像素计数用它当门限；拿 4095 判会把过曝帧判成"没饱和"（踩过，白跑一轮）。
TL_SATURATION_ADU = 1022
# 预览显示朝向（0/90/180/270，顺时针）。**只转预览**：保存的文件永远是传感器原始朝向，
# 质心的**读数**也永远是传感器坐标（在未旋转的那一帧上算）；只有十字线跟着预览朝向画。
TL_PREVIEW_ROTATION = int(os.getenv("PI_CCD_ROTATION", "0"))
TL_OPEN_TIMEOUT_S = 15.0     # 打开相机（含固件握手）的上限
TL_CAPTURE_TIMEOUT_S = 30.0  # 单帧采集上限（含曝光）

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
