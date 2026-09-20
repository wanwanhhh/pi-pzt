# PI P-621.1CD 位移台控制台

单机单用户。浏览器控制 PI E-709.CRG + P-621.1CD 压电台：
手动点到点、自动等间隔扫描，每点停稳后采图，实际位置作为该点元数据入库。

相机可选：索雷博 CS165MU（Zelux）走官方 Python SDK，界面第三个页签提供实时预览（画面上叠质心十字线与
读数（**始终是传感器坐标，与保存的文件同一套**）、预览朝向可 90° 一档旋转，**只转预览、不动保存的文件**）、
手动保存原始帧（16 位全幅 PNG）与改名（改的是磁盘上的文件名）；**扫描每点采图与手动保存用的是同一条通路**。

## 环境

- Windows 或 Linux，Python 3.12+
- **Windows**：PI Software Suite 2.8.2.0（提供 GCS DLL 与 USB 驱动），走 `ConnectUSB`
- **Linux**：**无需安装任何 PI 软件**。内核 `ftdi_sio` 直接提供 FTDI 虚拟串口，
  PIPython 以 `PISerial` 走纯 Python 命令层，不加载 GCS DLL。当前用户需在
  `dialout` 组才能打开串口：

      sudo usermod -aG dialout $USER     # 执行后必须重新登录才生效

  临时绕过（不重新登录）：`sg dialout -c './run.sh'`
- 控制器接 USB。**PIMikroMove 必须关闭**，设备同一时刻只能有一个占用者。

### 相机（可选，仅 Windows）

索雷博 CS165MU 走官方 Python SDK `thorlabs_tsi_sdk`（ctypes 包原生 DLL）：

1. 装厂家软件包 **Thorlabs Scientific Imaging Software**（提供 ThorCam、USB 驱动与 Python SDK）。
2. **Python SDK 不在 PyPI 上**，只能从安装包解出来的 SDK 里装：

       .venv\Scripts\python.exe -m pip install "<SDK 路径>\Scientific Camera Interfaces\SDK\Python Toolkit\thorlabs_tsi_camera_python_sdk_package.zip"

   （同一份源码也在该目录的 `source\` 下，不想安装可以把它塞进 `sys.path`。）
3. 原生 DLL 在 `SDK\Python Toolkit\dlls\64_lib`，用环境变量 `TL_SDK_DLLS` 指过去。
   **设备层会按全路径预加载全部 19 个 DLL，所以不要求你先把它加进 PATH**；
   `run_camera.bat` 里仍然设 PATH，那是给 `tools/` 下的脚本兜底。
4. **ThorCam 必须关闭** —— 相机同一时刻只能有一个占用者（跟 PIMikroMove 的规矩一样）。
5. 先自检一次，确认能开相机、能出帧：

       .venv\Scripts\python.exe tools\hwtest_ccd.py --preview

   报"打不开"时先看 `tools\thorlabs_probe.py` 的输出：它会把裸 SDK 的错误码与缺哪个 DLL 逐条打出来。

## 安装

    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt

    Windows 把上面两行换成：
        python -m venv .venv
        .venv\Scripts\python.exe -m pip install -r requirements.txt

## 运行

    ./run.sh            # Linux
    run.bat             # Windows（只有位移台）
    run_camera.bat      # Windows，带索雷博相机（设好 PI_CCD=thorlabs 与 TL_SDK_DLLS）

界面：http://127.0.0.1:8000　（「对准」/「扫描」/「预览」三个页签）

## 测试

    .venv/bin/python tools/hwtest.py     # 设备层，会真实驱动位移台
    .venv/bin/python tools/apitest.py    # 接口回归，需先启动服务，也会驱动位移台
    node tools/uitest.js                 # 前端逻辑，假 DOM，不碰硬件

    Windows 把 .venv/bin/python 换成 .venv\Scripts\python.exe

Windows 上相机另有两个脚本（不驱动位移台）：

    .venv\Scripts\python.exe tools\thorlabs_probe.py           # 联通性：能不能发现相机、DLL 齐不齐
    .venv\Scripts\python.exe tools\hwtest_ccd.py --preview     # 上机自检：采一帧、存 16 位 PNG、打统计量

上面三个都不依赖测试框架，直接跑。另有 9 个离线单测（不碰硬件，也不依赖测试框架，逐个直接跑）：
`backend/tests/test_xmt_protocol.py` 协议编解码、`test_stage_api.py` 设备层契约、
`test_store_migration.py` 库结构迁移、`test_xmt_check_safety.py` 自检脚本护栏、
`test_xmt_stage.py` XMT 设备层（假串口）、`test_xmt_link.py` 帧收发与帧间隔（假串口）、
`test_scanner_arrival.py` 扫描的到位校验（假设备 + 临时库）、
`test_trace.py` 位置曲线的环形缓冲（窗口两端闭合、按时间裁剪、增长有界）、
`test_centroid.py` 预览质心的定义与边界（权重按强度、全零图无质心、饱和计数、与暴力法逐位一致）。
`hwtest.py` 覆盖连接/点到点/中途停止/急停/释放；
`apitest.py` 覆盖位置曲线、伺服、手动、扫描、暂停继续中止、停止中止扫描、急停收尾共 11 组；
`uitest.js` 用假 DOM 把 `app.js` 真跑起来，覆盖点位载入状态机、乱序保护、按钮可用性、位置曲线取数与三页签切换
（覆盖边界写在它的文件头，别当整页渲染回归用）。

## 结构

    backend/     Python 后端（分层见 backend/__init__.py）
    frontend/    单页界面，零构建
    tools/       上机自检脚本
    docs/        PI 手册与 GCS 命令表、docs/pi 与 docs/xmt 与 docs/thorlabs 的设备认识账
    data/        运行期生成：SQLite 元数据 + 图像

## 配置

可调参数集中在 backend/config.py。行程范围不写死，连上设备后从 TMN? / TMX? 读取。

设备连接方式由环境变量控制（详见 config.py）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PI_LINK` | `auto` | `auto` 时 Linux 走串口、Windows 走 USB；可强制 `usb` / `serial` |
| `PI_SERIAL_PORT` | 空 | 串口路径。留空则自动找 `/dev/serial/by-id/usb-PI_*` |
| `PI_SERIAL_BAUD` | `115200` | 串口波特率 |
| `PI_SERIAL` | 空 | **仅 USB 通道**的序列号过滤，串口通道不使用 |
| `PI_CCD` | `dummy` | CCD 后端：`null` 不采图 / `dummy` 占位灰度图 / `thorlabs` 真实相机 |
| `TL_SDK_DLLS` | 本机 SDK 路径 | 厂商原生 DLL 目录（`dlls\64_lib`） |
| `PI_CCD_EXPOSURE_US` | `12000` | 曝光（µs）的**启动默认值**；**预览与采图共用一个**。界面上可实时改 |
| `PI_CCD_GAIN` | `0` | 增益固定 0：它只放大信号+噪声，不改善信噪比；接口拒绝修改 |
| `PI_CCD_PREVIEW_FPS` | `15` | 预览取帧节拍（fps）。只影响预览，不碰采图 |
| `PI_CCD_JPEG_QUALITY` | `80` | 预览 JPEG 质量。只影响预览，保存的 PNG 是无损 16 位，与它无关 |
| `PI_CCD_ROTATION` | `0` | **预览**显示朝向（0/90/180/270，顺时针），界面上可随时改。保存的文件永远是传感器朝向 |

关于成像参数：**曝光可以随时调**（界面上改完立刻生效，预览流不停），**预览与采图共用同一个值**
——两边不一致的话，"预览里看着挺好"和"存下来的"就是两张亮度不同的图。
**每一帧保存时把相机读回的实际曝光写进 PNG 自己的 tEXt 块**（键名 `ExposureUs`），
**质心也一样写进去**（键名 `CentroidPx`，值是 "cx,cy"，**传感器坐标**，与文件里的像素同一套），
列表里直接显示（两行：曝光 / 质心），拷到别处也问得出来；老图没有这两个块就显示"未记录"，不猜值。相机这一路的实测结论与踩坑见 `docs/thorlabs/设备认识账.xml`。
