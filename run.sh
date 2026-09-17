#!/usr/bin/env bash
# PI P-621.1CD 位移台控制台（Linux）
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
export PYTHONUTF8=1

if [ ! -x ".venv/bin/python" ]; then
    echo "找不到 .venv，请先执行："
    echo "    python3 -m venv .venv"
    echo "    .venv/bin/python -m pip install -r requirements.txt"
    exit 1
fi

if ! id -nG | tr ' ' '\n' | grep -qx dialout; then
    echo "警告：当前账户不在 dialout 组，打不开串口设备，界面会显示「未连接」。"
    echo '    sudo usermod -aG dialout $USER    # 执行后需重新登录才生效'
    echo "    临时绕过：sg dialout -c './run.sh'"
    echo
fi

echo "界面地址：http://127.0.0.1:8000"
echo "关闭本窗口即停止服务；位移台保持原位不动（要卸力请点界面上的“释放”）。"
exec .venv/bin/python -m backend.server
