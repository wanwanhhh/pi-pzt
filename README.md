# PI P-621.1CD 位移台控制台

单机单用户。浏览器控制 PI E-709.CRG + P-621.1CD 压电台：
手动点到点、自动等间隔扫描，每点停稳后采图，实际位置作为该点元数据入库。

## 环境

- Windows + Python 3.14
- PI Software Suite 2.8.2.0（提供 GCS DLL 与 USB 驱动）
- 控制器接 USB。**PIMikroMove 必须关闭**，设备同一时刻只能有一个占用者。

## 安装

    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt

## 运行

    run.bat

界面：http://127.0.0.1:8000

## 测试

    .venv\Scripts\python.exe tools\hwtest.py     # 设备层，会真实驱动位移台
    .venv\Scripts\python.exe tools\apitest.py    # 接口回归，需先启动服务，也会驱动位移台
    node tools\uitest.js                          # 前端逻辑，假 DOM，不碰硬件

三个都不依赖测试框架，直接跑。`hwtest.py` 覆盖连接/点到点/中途停止/急停/释放；
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
