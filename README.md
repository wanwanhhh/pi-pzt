# PI P-621.1CD 位移台控制台

单机单用户。浏览器控制 PI E-709.CRG + P-621.1CD 压电台：
手动点到点、自动等间隔扫描，每点停稳后采图，实际位置作为该点元数据入库。

## 环境

- Windows 或 Linux，Python 3.12+
- **Windows**：PI Software Suite 2.8.2.0（提供 GCS DLL 与 USB 驱动），走 `ConnectUSB`
- **Linux**：**无需安装任何 PI 软件**。内核 `ftdi_sio` 直接提供 FTDI 虚拟串口，
  PIPython 以 `PISerial` 走纯 Python 命令层，不加载 GCS DLL。当前用户需在
  `dialout` 组才能打开串口：

      sudo usermod -aG dialout $USER     # 执行后必须重新登录才生效

  临时绕过（不重新登录）：`sg dialout -c './run.sh'`
- 控制器接 USB。**PIMikroMove 必须关闭**，设备同一时刻只能有一个占用者。

## 安装

    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt

    Windows 把上面两行换成：
        python -m venv .venv
        .venv\Scripts\python.exe -m pip install -r requirements.txt

## 运行

    ./run.sh        # Linux
    run.bat         # Windows

界面：http://127.0.0.1:8000

## 测试

    .venv/bin/python tools/hwtest.py     # 设备层，会真实驱动位移台
    .venv/bin/python tools/apitest.py    # 接口回归，需先启动服务，也会驱动位移台
    node tools/uitest.js                 # 前端逻辑，假 DOM，不碰硬件

    Windows 把 .venv/bin/python 换成 .venv\Scripts\python.exe

上面三个都不依赖测试框架，直接跑。另有 7 个离线单测（不碰硬件，也不依赖测试框架，逐个直接跑）：
`backend/tests/test_xmt_protocol.py` 协议编解码、`test_stage_api.py` 设备层契约、
`test_store_migration.py` 库结构迁移、`test_xmt_check_safety.py` 自检脚本护栏、
`test_xmt_stage.py` XMT 设备层（假串口）、`test_xmt_link.py` 帧收发与帧间隔（假串口）、
`test_scanner_arrival.py` 扫描的到位校验（假设备 + 临时库）。`hwtest.py` 覆盖连接/点到点/中途停止/急停/释放；
`apitest.py` 覆盖伺服、手动、扫描、暂停继续中止、停止中止扫描、急停收尾共 10 组；
`uitest.js` 用假 DOM 把 `app.js` 真跑起来，覆盖点位载入状态机、乱序保护与按钮可用性
（覆盖边界写在它的文件头，别当整页渲染回归用）。

## 结构

    backend/     Python 后端（分层见 backend/__init__.py）
    frontend/    单页界面，零构建
    tools/       上机自检脚本
    docs/        PI 手册与 GCS 命令表
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
| `PI_CCD` | `dummy` | CCD 后端，接真相机时替换 |
