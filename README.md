# PI P-621.1CD 位移台控制台

浏览器控制压电台：手动点到点、自动等间隔扫描，每点停稳后采图，实际位置作为该点元数据入库。
位移台可选 PI E-709.CRG + P-621.1CD（Linux / Windows）或芯明天 E53.D1S-H（仅 Windows），
配置里选一台；相机可选（索雷博 CS165MU）。
界面：http://127.0.0.1:8000 ——「对准」/「扫描」/「预览」三个页签。

**本文件只讲环境与启动。** 需求与设计意图见 `AGENTS.md`；设备细节、实测数字与踩坑见 `docs/` 下各设备的
「认识账」（`docs/pi` / `docs/xmt` / `docs/thorlabs`）。

## 快速开始

Linux：

    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
    ./run.sh

Windows：

    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    run.bat

浏览器打开 http://127.0.0.1:8000。

**控制器没接也能起来**：设备连接失败只记一条日志，服务照常跑，界面显示「未连接」；接好后点界面上的
「重连」即可，不用重启服务。**相机默认不启用**（`PI_CCD=dummy`），「预览」页整个不可用 ——
要真相机用 `run_camera.bat`（见下）。

## 环境

- Windows 或 Linux，Python 3.12+
- 控制器接 USB。**PIMikroMove 必须关闭** —— 设备同一时刻只能有一个占用者（相机同理，见下）。

### 位移台：PI E-709.CRG + P-621.1CD

- **Windows**：装 PI Software Suite 2.8.2.0（提供 GCS DLL 与 USB 驱动），走 `ConnectUSB`。
- **Linux**：**不需要装任何 PI 软件**。内核 `ftdi_sio` 直接提供 FTDI 虚拟串口，PIPython 以
  `PISerial` 走纯 Python 命令层，不加载 GCS DLL。当前用户需在 `dialout` 组才能打开串口：

      sudo usermod -aG dialout $USER     # 执行后必须重新登录才生效

  临时绕过（不重新登录）：`sg dialout -c './run.sh'`

### 位移台：芯明天 E53.D1S-H（可选，仅 Windows）

自写协议 + pyserial 直连 USB CDC 虚拟串口（`VID_0483&PID_0002`，系统自带 `usbser.sys`，
**不需要厂家驱动**），按 VID:PID 自动找口、不写死 COM7。启动前设 `PI_DEVICE=xmt`
（启动脚本不替你设）：

    set PI_DEVICE=xmt && run.bat

协议与厂家资料见 `docs/xmt/README.md`，实测与经验见 `docs/xmt/设备认识账.xml`。

### 相机（可选，仅 Windows）

索雷博 CS165MU 走官方 Python SDK `thorlabs_tsi_sdk`（ctypes 调原生 DLL）：

1. 装厂家软件包 **Thorlabs Scientific Imaging Software**（提供 ThorCam、USB 驱动与 Python SDK）。
2. **Python SDK 不在 PyPI 上**，只能从安装包解出来的 SDK 里装：

       .venv\Scripts\python.exe -m pip install "<SDK 路径>\Scientific Camera Interfaces\SDK\Python Toolkit\thorlabs_tsi_camera_python_sdk_package.zip"

   （同一份源码也在该目录的 `source\` 下，不想安装可以把它塞进 `sys.path`。）
3. 原生 DLL 在 `SDK\Python Toolkit\dlls\64_lib`，用环境变量 `TL_SDK_DLLS` 指过去。
   **设备层会按全路径预加载全部 19 个 DLL，不要求你先加进 PATH**；`run_camera.bat` 里仍然设 PATH，
   那是给 `tools/` 下的脚本兜底。
4. **ThorCam 必须关闭** —— 相机同一时刻只能有一个占用者。
5. 先自检一次，确认能开相机、能出帧：

       .venv\Scripts\python.exe tools\hwtest_ccd.py --preview

   报「打不开」时先看 `tools\thorlabs_probe.py` 的输出：它会把裸 SDK 的错误码与缺哪个 DLL 逐条打出来。

## 启动与验证

    ./run.sh          # Linux（只有位移台）
    run.bat           # Windows（只有位移台）
    run_camera.bat    # Windows，带索雷博相机（设好 PI_CCD=thorlabs 与 TL_SDK_DLLS）

前台进程，Ctrl+C 或关窗口即停止；位移台保持原位不动（要卸力点界面上的「释放」）。
等价的直接启动：`.venv/bin/python -m backend.server`
（Windows：`.venv\Scripts\python.exe -m backend.server`）。**workers 必须是 1** ——
设备 owner 线程不能跨进程复制，别自己加 `--workers`。

启动后界面右上角有四个状态灯（界面 / 设备 / 伺服 / 扫描）。控制器没上电时「设备」是「未连接」，其余照常。

**不碰硬件的自检**（新装完先跑这个）：逐个跑 `backend/tests/` 下的文件，Linux 一条循环：

    for f in backend/tests/test_*.py; do .venv/bin/python "$f" || break; done

离线单测不依赖测试框架，也不碰硬件：设备层契约、XMT 协议编解码与假串口、扫描到位校验、
库结构迁移、位置曲线环形缓冲、预览质心。

**上机自检**（会真实驱动位移台，跑之前先看脚本头的说明）：

    .venv/bin/python tools/hwtest.py     # 设备层：连接 / 点到点 / 中途停止 / 急停 / 释放
    .venv/bin/python tools/apitest.py    # 接口回归：需先启动服务（端口写死 127.0.0.1:8000）
    node tools/uitest.js                 # 前端逻辑：假 DOM 跑 app.js，不碰硬件

Windows 把 `.venv/bin/python` 换成 `.venv\Scripts\python.exe`。相机另有两个脚本
（`tools\thorlabs_probe.py` 连通性、`tools\hwtest_ccd.py --preview` 采帧自检），都不驱动位移台。

## 环境变量

全部可调参数集中在 `backend/config.py`；行程范围不写死，连上设备后从控制器读取。

**通用**

| 变量 | 默认 | 说明 |
|---|---|---|
| `PI_PORT` | `8000` | 监听端口（地址固定 `127.0.0.1`） |

**选设备**

| 变量 | 默认 | 说明 |
|---|---|---|
| `PI_DEVICE` | `pi` | `pi` = PI E-709 / `xmt` = 芯明天 E53.D1S-H；同时只激活一台 |
| `PI_DEVNAME` | `E-709` | GCS 设备名（`PI_DEVICE=pi` 时用） |
| `PI_AXIS` | `X` | 轴名 |

**PI 接入**

| 变量 | 默认 | 说明 |
|---|---|---|
| `PI_LINK` | `auto` | `auto` 时 Linux 走串口、Windows 走 USB；可强制 `usb` / `serial` |
| `PI_SERIAL_PORT` | 空 | 串口路径。留空则自动找 `/dev/serial/by-id/usb-PI_*` |
| `PI_SERIAL_BAUD` | `115200` | 串口波特率 |
| `PI_SERIAL` | 空 | **仅 USB 通道**的序列号过滤；串口通道不使用 |

**芯明天 XMT**

| 变量 | 默认 | 说明 |
|---|---|---|
| `PI_XMT_PORT` | 空 | 串口路径。留空则按 VID:PID 找 |
| `PI_XMT_BAUD` | `115200` | 串口波特率 |
| `PI_XMT_ADDR` | `1` | 设备地址 |

**相机**

| 变量 | 默认 | 说明 |
|---|---|---|
| `PI_CCD` | `dummy` | `null` 不采图 / `dummy` 占位灰度图 / `thorlabs` 真实相机 |
| `TL_SDK_DLLS` | 本机 SDK 路径 | 厂商原生 DLL 目录（`dlls\64_lib`） |

曝光、预览帧率、预览朝向这些**运行期参数**（`PI_CCD_EXPOSURE_US` / `PI_CCD_PREVIEW_FPS` /
`PI_CCD_JPEG_QUALITY` / `PI_CCD_ROTATION` / `PI_CCD_GAIN`）在 `backend/config.py` 里，
界面上也都能改 —— 这里不抄它们的默认值，免得改一处漏一处。

## 起不来时先看这几条

- **界面一直「未连接」**：控制器上电、USB 插好；Linux 上看当前用户是否在 `dialout` 组（见上）；
  **PIMikroMove / ThorCam 是否还开着**。接好后点「重连」，不用重启服务。
- **端口被占**：`PI_PORT=8001 ./run.sh`，再开 http://127.0.0.1:8001 。
- **XMT 起不来**：确认在 Windows 上，且设了 `PI_DEVICE=xmt`。
- **相机打不开**：先跑 `tools\thorlabs_probe.py`，它会指明缺哪个 DLL 或返回哪个错误码。

## 目录结构

    backend/     Python 后端（分层见 backend/__init__.py）与离线单测 backend/tests/
    frontend/    单页界面，零构建
    tools/       上机自检脚本
    docs/        设备手册与各设备的「认识账」（docs/pi、docs/xmt、docs/thorlabs）
    data/        运行期生成：SQLite 元数据 + 图像（不入库）
