"""xmt_check 的安全护栏自检：脚本发的每条命令都必须在白名单里。

这个文件存在的理由很具体：白名单漏了 CMD_READ_POSITION，脚本跑到阶段 2.5
才被自己的护栏拦下 —— 拦对了，但整趟白跑。手工维护的集合必须有人核对，
而三轮人工复审都没看出来。

**只管 tools/xmt_check.py**（只读自检那份）。会动的 xmt_move.py / xmt_scale.py
自己声明 ALLOW 并在调用点上写明，不在本文件的管辖范围内。

做法是 AST 解析源码，**不 import 被测脚本**：它是上机脚本，import 会拖进 pyserial，
离线测试不该依赖串口栈，也不该执行它。四条不变式：

  1. 除 SAFE 定义之外引用的每个 xp.CMD_*，其值都必须在 SAFE 里
  2. SAFE 里的每一项都必须真的被用到（不留死条目）
  3. SAFE ⊆ 已知只读命令（协议日后新增的写命令混不进来）
  4. 任何 .send(...) 调用都不许带第二个位置参数或 allow=

第 4 条是关键：Link.send 的 allow 是给会动的兄弟脚本用的。只要本文件里一处
allow 都没有，send() 里那句"值不在 SAFE 就 raise"就成了**不可绕过的**检查 ——
连手写魔法数 link.cmd(1, ...) 也会在运行时被拦住。第 1 条只管名字形状，
第 4 条把它补成值域检查。

直接跑：python backend/tests/test_xmt_check_safety.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend import xmt_protocol as xp  # noqa: E402

SOURCE = (ROOT / "tools" / "xmt_check.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)

# 允许出现在只读白名单里的命令名。新增命令必须在这里登记一遍 ——
# 登记这个动作就是"人确认过它是只读的"。
READ_ONLY = frozenset((
    "CMD_READ_ADDRESS", "CMD_HANDSHAKE", "CMD_MODEL", "CMD_READ_UNIT",
    "CMD_READ_LOOP_MODE", "CMD_READ_POSITION", "CMD_READ_POS_LIMIT_HIGH",
    "CMD_READ_POS_LIMIT_LOW", "CMD_STREAM_POSITION", "CMD_STOP_STREAM",
    "CMD_POWER_INFO", "CMD_STAGE_INFO",
))


def _cmd_names(node: ast.AST) -> set[str]:
    """子树里出现的所有 xp.CMD_* 名字。"""
    return {
        n.attr
        for n in ast.walk(node)
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id == "xp"
        and n.attr.startswith("CMD_")
    }


def _safe_assignment() -> ast.Assign:
    for node in TREE.body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "SAFE" for t in node.targets
        ):
            return node
    raise AssertionError("tools/xmt_check.py 里找不到 SAFE 的定义")


SAFE_NODE = _safe_assignment()
SAFE_NODE_IDS = {id(n) for n in ast.walk(SAFE_NODE)}
SAFE_NAMES = _cmd_names(SAFE_NODE)
SAFE = frozenset(getattr(xp, n) for n in SAFE_NAMES)

# 白名单**之外**引用到的命令
USED_OUTSIDE = {
    n.attr
    for n in ast.walk(TREE)
    if id(n) not in SAFE_NODE_IDS
    and isinstance(n, ast.Attribute)
    and isinstance(n.value, ast.Name)
    and n.value.id == "xp"
    and n.attr.startswith("CMD_")
}

SEND_CALLS = [
    n for n in ast.walk(TREE)
    if isinstance(n, ast.Call)
    and isinstance(n.func, ast.Attribute)
    and n.func.attr == "send"
]


def test_every_sent_command_is_whitelisted():
    leaked = sorted(n for n in USED_OUTSIDE if getattr(xp, n) not in SAFE)
    assert not leaked, f"这些命令会发出去但不在 SAFE 里: {leaked}"


def test_whitelist_has_no_dead_entries():
    dead = sorted(n for n in SAFE_NAMES if n not in USED_OUTSIDE)
    assert not dead, f"SAFE 里这些条目根本没被用到: {dead}"


def test_whitelist_contains_only_registered_read_commands():
    """白名单里不许出现没登记过的命令（也就挡住了新写命令混进来）"""
    extra = sorted(SAFE_NAMES - READ_ONLY)
    assert not extra, f"SAFE 里有没登记的命令: {extra}（确认只读后加进 READ_ONLY）"


def test_no_call_widens_the_whitelist():
    """本文件里任何 .send(...) 都不许带第二位置参数或 allow= 关键字。

    这一条与命令怎么拼写无关：只要一处 allow 都没有，send() 的值域检查就绕不过去。
    """
    bad = [ast.unparse(c) for c in SEND_CALLS if len(c.args) != 1 or c.keywords]
    assert not bad, f"这些 send 调用放宽了白名单（只许 send(帧)）: {bad}"


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"FAIL  {name}\n      {type(exc).__name__}: {exc}")
        else:
            print(f"ok    {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
