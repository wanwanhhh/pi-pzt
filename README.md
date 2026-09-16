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

## 上机自检

    .venv\Scripts\python.exe tools\hwtest.py

会真实驱动位移台：连接、点到点、中途停止、急停、释放伺服。

## 结构

    backend/     Python 后端（分层见 backend/__init__.py）
    frontend/    单页界面，零构建
    tools/       上机自检脚本
    docs/        PI 手册与 GCS 命令表
    data/        运行期生成：SQLite 元数据 + 图像

## 配置

可调参数集中在 backend/config.py。行程范围不写死，连上设备后从 TMN? / TMX? 读取。
