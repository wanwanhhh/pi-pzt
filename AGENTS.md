# AGENTS.md

## 规则

1. 未经用户允许，不得创建子代理（subagent）。
2. **未经用户允许，不得执行 git 提交、推送、reset 等会改动历史的操作。** 改文件、跑测试、上机自检不用问。
3. **写给人看的解释按需求方 / 用户的角度写，不按专业角度写**：先说这对使用者意味着什么，
   不从器件原理、协议时序这类地方切入；能列成「情况 / 表现 / 怎么办」的就用表格。
   **文档怎么写见 `docs/文档编写规范.md`**（四层体系、认领表、逐字引用、交付前自检）。

## 需求与设计在哪

**本文件是规程**：只写怎么干活（选型、约束、构建发布、红线），不写需求，也不重复设备数字。

- **要做到什么**（该不该做、砍掉什么）→ [`docs/需求.md`](docs/需求.md)。
- **每一层对外承诺什么效果、出什么事说什么** → [`docs/设计/`](docs/设计/)：设备层 / 扫描 / 相机 / 数据 / 界面。
- **有问题读哪份** → [`docs/地图.md`](docs/地图.md)；**一个词什么意思** → [`docs/词表.md`](docs/词表.md)。
- **动设备层代码前先读对应那一份认识账**（装置细节、实测数字、踩坑只在那里）：
  - PI E-709.CRG + P-621.1CD → [`docs/pi/设备认识账.xml`](docs/pi/设备认识账.xml)（接入与安装见 [`README.md`](README.md)）
  - 芯明天 E53.D1S-H + XD780.200S → [`docs/xmt/设备认识账.xml`](docs/xmt/设备认识账.xml)（协议与厂家资料见 [`docs/xmt/README.md`](docs/xmt/README.md)）
  - 索雷博 CS165MU（Zelux）相机 → [`docs/thorlabs/设备认识账.xml`](docs/thorlabs/设备认识账.xml)（SDK 安装见 [`README.md`](README.md)）
  - 认识账里标为「处置」的条目是硬要求。

## 技术栈

| 层             | 选型                                                         |
| -------------- | ------------------------------------------------------------ |
| 后端           | Python 3.12+ + FastAPI + uvicorn（`--workers 1`）             |
| 硬件 API（PI）  | PIPython。Windows 走 GCS DLL（`ConnectUSB`），Linux 走 FTDI 虚拟串口（`PISerial`，115200） |
| 硬件 API（XMT） | 芯明天 E53.D1S-H：**自写协议 + pyserial** 直连 USB CDC 虚拟串口（`VID_0483&PID_0002`，系统自带 `usbser.sys`，无需厂家驱动）。**仅 Windows**；协议与资料见 `docs/xmt/README.md`，实测与经验见 `docs/xmt/设备认识账.xml` |
| 硬件 API（相机）| 索雷博 CS165MU：`thorlabs_tsi_sdk`（厂家 SDK 本地包，ctypes 调原生 DLL；19 个 DLL 按全路径预加载）。**仅 Windows**；实测与踩坑见 `docs/thorlabs/设备认识账.xml` |
| 设备访问       | 专用 owner 线程 + `queue.Queue`（不用 `asyncio.to_thread`）    |
| 命令通道       | REST + Pydantic 校验                                          |
| 遥测通道       | SSE（不用 WebSocket）                                         |
| 前端           | 单页原生 HTML/JS，零构建                                       |
| 曲线           | uPlot                                                        |
| 存储           | 每点元数据 SQLite（WAL）；曲线 npz；图像由 CCD 库落盘            |
| 监听地址       | `127.0.0.1`                                                  |

不用：Streamlit、NiceGUI、Flask、React/Vue/Vite、Django、WebSocket、Redis/Celery/PostgreSQL。

## 结构

    backend/     Python 后端，只允许上层依赖下层：
                 server（路由/SSE） → scanner（扫描执行器） → store（SQLite） → ccd（采图接口）
                     → thorlabs_ccd（索雷博 CS165MU：owner 线程 + 原生全幅 16 位 PNG）
                 → stage_api（设备层公共面：能力声明 + 状态结构 + 契约）
                     → pi_stage（PI E-709）
                     → xmt_stage（芯明天 E53.D1S-H）   ← 配置里选一台，同时只激活一台
                 → config（全部可调参数）
                 trace（位置曲线环形缓冲：遥测线程写、HTTP 只读，不额外轮询设备）
                 tests/  离线单测，不碰硬件
    frontend/    单页原生 HTML/JS，零构建
    tools/       上机自检脚本
    docs/        需求、设计、词表、地图 + 各设备的资料与认识账（读哪份见 docs/地图.md）
    data/        运行期生成（SQLite + 图像），不入库

## 编码规范

高效、高质量、少防御性编程、少补丁。

> 例外：进硬件前的参数校验、软限位夹取、设备错误码检查是安全边界，必须保留。
