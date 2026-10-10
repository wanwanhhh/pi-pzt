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

# 「这条扫描的位置口径」= 采它的那台设备把位置报成什么单位：**µm = 读回值 × 系数**。
# 这是**设备属性**，不是全局常量：扫描时按当时那台设备记进 scan.readback_to_um；
# 加这一列之前的老数据没记，界面上可以手工标一次（标的是"这条是哪台设备采的"）——
# 库里的数一个字节都不改：原值那一列是按这个系数从已存的 µm 反推出来的。
# 界面上的选项与文案由 GET /api/readback-sources 下发（前端不写系数）。
READBACK_SOURCES = (
    # (键, 界面标签, 读回 → µm 系数)
    ("pi", "PI E-709（位置本来就是 µm）", 1.0),
    ("xmt", "XMT E53.D1S-H（读回 = 4/3 µm）", XMT_READBACK_TO_UM),
)
# 行程读数超出这个范围就认为读错了（设备没标定 / 串了口），拒绝连接
XMT_MAX_TRAVEL_UM = 1000.0

def raw_readback_factor(readback_to_um: float | None) -> float | None:
    """µm → **设备原值** 要除的那个系数；没有可给的就 None。

    两种"没有"：没记下这条扫描是哪台设备采的（加 readback_to_um 这一列之前的老数据）、
    系数本来就是 1（PI 报的位置就是 µm，原值 = µm，给一列一模一样的数没有信息）。
    拿 µm 冒充原值是编数，所以宁可不给 —— 调用方据此**禁用**而不是自己找个数顶上。
    数据页横轴那两列（position_raw / time_raw_fs）与导出 CSV 的 readback_raw 用的都是它：
    「什么时候有原值」只此一处，别在调用方各写一遍。
    """
    return readback_to_um if readback_to_um and readback_to_um != 1.0 else None

# **设备自报的行程不是可用行程。** 设备里没有任何标定数据（45/82/59 全空），
# 27/35 只是固件默认值，而且永不生效 —— 先到的是电压轨：设点 ≈159.4 µm 处驱动量
# 撞上它自己的上限 200，162 µm 就翻进「负位置 + 负驱动」，要下发 100 µm 才拉得回来。
# 下界同理：≲3 µm 报的不是测量值（σ<0.2 nm、格子 1e-4，都比一个 AD 码 3.225 nm 小），
# 2.4~2.8 µm 是过渡带。实测可用区间 ≈ 3.1~159 µm，这里取 5~150 留余量。
# 依据见 docs/xmt/设备认识账.xml 的 A3 / A5。
XMT_USABLE_MIN_UM = 5.0
XMT_USABLE_MAX_UM = 150.0

# 曾有一条「折算系数自检」诊断（读回确认没落在目标附近时，用两次确认之间的差分把
# 「像丢帧」还是「像系数变了」说清楚，见 docs/xmt/设备认识账.xml P0.5）。**已随读回确认
# 一起删掉**：扫描那条路现在等满稳定延时就直接采图，不再有确认那一步，诊断也就没有触发点。
# 留着它的代价是"每个点都在噪声水平的偏差上刷错误日志"，比它带来的信息更糟。

XMT_READ_TIMEOUT = 0.3      # 单条读命令等回包上限（s）
XMT_CMD_TIMEOUT = 5.0       # 单个 owner 任务上限（s）

# 到位判据：**扫描不做任何判据**（2026-09 用户定）。设备没有到位信号，做法就是
# 「等满界面上设的稳定延时，到点采图」—— 等多久由用户负责，旋钮动过要重设
# （见 docs/xmt/设备认识账.xml E12/E7）。**超差不拦、不中止、不标无效**：
# 丢帧（设点无应答）因此是静默的，事后只能从每点记下的读数看出来。
XMT_ARRIVAL_TOL_UM = 0.2
# 这个容差现在只剩**显示与记录**两处用途：界面「到位」列（= 最近一次读数是否落在
# 目标 ±容差内）、每点元数据里的 on_target（采图那一刻读数的比较）。它不再拦任何东西。
# 数值来历：实测偏差 = 单次读数噪声 σ 0.03 µm + 台子自己的稳定偏置（设点 22.0 µm 时
# +0.073 µm，220 s 不收敛；145 µm 段变号成 −0.04），最坏 0.12 µm，所以取 0.2；
# 偏置随区域变号，不能拿常数补掉（口径见设备认识账 E11）。

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
# **首点的最小稳定延时（ms）**，两台设备共用。
# 起点贴着行程端点时预逼近退不出去（_approach_start 会记一条警告），首点就是从别处
# 直接过来的 —— 而控制器的 ONT? 会在台子还在整定时就置位：2026-10 实测一次 90 µm 移动，
# ONT 在 MOV 后 61 ms 置位，此时离目标还差 0.32 µm；+230 ms 差 0.042、+340 ms 差 0.018、
# +450 ms 才进 0.013 µm。半个步距的校验（3000 点扫 100 µm 时只有 0.0167 µm）对首点因此
# 是掷骰子 —— 库里 #70/#72/#74 三条都是「第 0 点偏差超半个步距」失败的。
# 首点等满这个延时再采，其余点照旧用界面上设的稳定延时。500 是照上面那条曲线留的余量。
FIRST_POINT_SETTLE_MS = 500
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
# **唯一的一套 ROI**：预览、手动保存、扫描共用原生全幅，一个像素都不裁
# —— 预览里看到的就是存下来的那一幅。ROI 会被硬件按对齐改写，所以一律按读回值记账。
# 早先预览另设过左上角 520x520（帧率 70 vs 35 fps），但那样预览只显示全幅的 18%，
# 容易误以为"视野就这一块"；而且两套 ROI 意味着保存时要重配相机，实测那一下会把
# 连续出帧弄停摆（认识账 A7 与 backend/thorlabs_ccd.py 文件头第 5 条）。所以不再提供。
TL_FULL_ROI = (0, 0, 1440, 1080)
TL_PREVIEW_FPS = float(os.getenv("PI_CCD_PREVIEW_FPS", "15"))
TL_JPEG_QUALITY = int(os.getenv("PI_CCD_JPEG_QUALITY", "80"))
# 满量程：**实测值**（认识账 A3）——SDK 交出来的最大值是 1022，不是 4095 也不是 65535。
# 饱和像素计数用它当门限；拿 4095 判会把过曝帧判成"没饱和"（踩过，白跑一轮）。
TL_SATURATION_ADU = 1022
# 预览显示朝向（0/90/180/270，顺时针）。**只转预览**：保存的文件永远是传感器原始朝向，
# 质心的**读数**也永远是传感器坐标（在未旋转的那一帧上算）；只有十字线跟着预览朝向画。
TL_PREVIEW_ROTATION = int(os.getenv("PI_CCD_ROTATION", "0"))
TL_OPEN_TIMEOUT_S = 15.0     # 打开相机（含固件握手）的上限
# 建会话时等设备出现多久。手动「开始预览」/「重开相机」给人留余量：拔插之后 USB 要重新
# 枚举，实测 1~3 s（认识账 E6/E10），8 s 是猜的余量 —— 界面上要说「正在打开」。
TL_OPEN_WAIT_S = 8.0
# 扫描前建会话不等：扫描是程序发起的，设备不在就该立刻失败，别把扫描卡在那儿。
TL_SCAN_OPEN_WAIT_S = 0.0
TL_CAPTURE_TIMEOUT_S = 30.0  # 单帧采集上限（含曝光）

# ---------------- 存储 ----------------
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "scans.db"
IMAGE_DIR = DATA_DIR / "images"
# 导出（把一条扫描的原始帧与每点元数据抄一份出去）落在这里，**跟 images/ 分开**：
# 删扫描、裁剪都不碰这里，这里的东西也不参与任何取数 —— 它只是"抄出去的那一份"。
EXPORT_DIR = DATA_DIR / "export"

# ---------------- 数据处理页：位置 → 时间 ----------------
# 数据页横轴的另一种读法。台子走 x µm，光在干涉仪里**往返**一趟，光程差就是 OPD = 2x µm；
# 这段光程差除以光速就是延迟 t = OPD / c。
#   c = 299792458 m/s = 0.299792458 µm/fs
#   → 1 µm 位移 = 2 / 0.299792458 = 6.6713 fs（其中那个 2 就是下面的 OPD_FACTOR）
# **跟波长无关**：绕道按波长算也一样，(2x/λ)·(λ/c) = 2x/c —— λ 自己约掉了，所以时间轴不设波长。
# OPD_FACTOR = 2 说的是「光走两次」（动镜往返）。**光只单程走一次的光路要改成 1** ——
# 改之前先确认光路，别照抄。
OPD_FACTOR = 2.0
C_UM_PER_FS = 0.299792458
TIME_FS_PER_UM = OPD_FACTOR / C_UM_PER_FS

# 频谱图把横轴从频率（THz）换成波长（µm）时用的系数：**λ[µm] = 这个数 ÷ ν[THz]**。
# 就是 c 换了个单位（1 THz 的光波长 299.792458 µm）—— 数值 = C_UM_PER_FS × 1e3。
# 跟 OPD_FACTOR 无关：波长是**光自己的**性质，不取决于干涉仪走几趟。
WAVELENGTH_UM_PER_THZ = C_UM_PER_FS * 1e3

# ---------------- 数据页：裁剪 ----------------
# 把**一组扫描**（同名的那几条）里每一帧都裁成同一块矩形，就地换掉原图（文件名不变）。
# 为什么值得：PNG 没有"只解一块"的办法，取数必须整幅解码 —— 解码代价基本与像素数成正比，
# 实测 1440×1080 → 400×300 后单张从 2.4 ms 降到 0.15 ms，2000 张从 4.8 s 到 0.3 s。
# **再裁小就没用了**：每张图还有约 0.11 ms 的固定开销（打开文件、建对象），
# 地板是 0.22 s / 2000 张（裁到 200×150 也只快 0.06 s），所以别为了快把框越缩越小。
# 坐标口径不因裁剪而改变：库里和 PNG 里都记 (x0,y0,w,h)，只在**读文件那一层**做一次减法，
# 对外（接口、界面、曲线、剖面）永远是**原始坐标**。裁过的和没裁过的可以混着比。
CROP_W = 400
CROP_H = 300
CROP_EDGE_PX = 150      # 建议框在质心范围外至少留这么多像素
CROP_SAMPLES = 40       # 算建议框时抽样多少帧（整组质心范围，逐帧读太慢也没必要）
# 重编码的压缩级别：level=1 比默认(6)快 2.5 倍（实测 23 → 9 ms/张），文件只大 10%。
# 这一刀是一次性的（一条 2000 点的扫描约 17 s），但没理由不快。
CROP_PNG_LEVEL = 1
# 扫描每点那一帧的 PNG 压缩级别。**全幅暗场帧上实测**（2026-10-10，scan0077 的一帧
# 1440×1080/16 位，同一份像素逐级重编码）：
#   level=1 → 13 ms / 1.14 MB      level=2 → 17 ms / 0.65 MB
#   level=3 → 24 ms / 0.56 MB      level=4 → 33 ms / 0.54 MB
#   level=6 → 67 ms / 0.52 MB（旧默认，实测扫描里 69 ms）
# 取 2：比 6 每帧省 50 ms，文件只大 25%。level=1 只再快 4 ms 却大一倍多 ——
# 「level=1 慢 2.5 倍、大 10%」那条是 400×300 裁剪帧上量的，**不能套到全幅**（见相机认识账 A7）。
# 与 CROP_PNG_LEVEL 分开写：两条路各调各的，别互相牵连。
SCAN_PNG_LEVEL = 2

# ---------------- 服务 ----------------
STATIC_DIR = BASE_DIR / "frontend"
HOST = "127.0.0.1"
PORT = int(os.getenv("PI_PORT", "8000"))
# 心跳只用于界面显示"前端在线"，不用于停止扫描（扫描独立于浏览器）
HEARTBEAT_TIMEOUT_S = 3.0
