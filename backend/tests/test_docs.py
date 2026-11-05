"""文档对账：认领表与需求逐字对上，引用不指向不存在的文件。

钉的是 docs/文档编写规范.md 第七节那几条：引用逐字、认领格两种写法、改名后全仓旧名清查。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
MD = sorted(list(ROOT.glob("*.md")) + list(DOCS.rglob("*.md")))

ENTRY = re.compile(r"^- \*\*「([^」]+)」（(?:必须|应该|可选)）。\*\*", re.M)


def _entries() -> list[str]:
    return ENTRY.findall((DOCS / "需求.md").read_text(encoding="utf-8"))


def _claims() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for p in sorted((DOCS / "设计").glob("*.md")):
        body = p.read_text(encoding="utf-8")
        start = body.index("## 这份设计认领了哪几条需求")
        table = body[start:].split("\n## ", 1)[0]
        for line in table.splitlines():
            if line.startswith("| 「"):
                out.setdefault(p.name, []).extend(re.findall(r"「([^」]+)」", line.split("|")[1]))
    return out


def test_claim_names_are_verbatim():
    entries = set(_entries())
    bad = [(f, n) for f, ns in _claims().items() for n in ns if n not in entries]
    assert not bad, f"认领表里这些名字在需求里找不到逐字条目：{bad}"


def test_every_requirement_claimed():
    claimed = {n for ns in _claims().values() for n in ns}
    missing = [n for n in _entries() if n not in claimed]
    assert not missing, f"这些需求没有任何设计认领：{missing}"


def test_claim_cells_do_not_contradict_the_requirement():
    """认领格写的效果不许和需求**相反** —— 名字对上只是第一步。

    这次抓到的一处：界面.md 的认领格写「灰度 dB 显示」，而需求里逐字写的是「按**线性**灰度显示
    （不做对数）」，同一份文档的正文又写的是线性。只比名字的对账永远发现不了这种。

    做法：需求里出现「线性灰度」的那些条目，认领格里同一行**不许出现 dB**（除非同时写了线性）。
    """
    body = (DOCS / "需求.md").read_text(encoding="utf-8")
    # 逐条切（条目可能跨行，不能按行正则）
    linear = set()
    for block in re.split(r"(?m)^- \*\*「", body)[1:]:
        name, _, rest = block.partition("」")
        if "线性" in rest:
            linear.add(name)
    assert linear, "需求里应当有写着「线性」的条目（否则这条对账没在查东西）"
    bad = []
    for p in sorted((DOCS / "设计").glob("*.md")):
        text = p.read_text(encoding="utf-8")
        start = text.index("## 这份设计认领了哪几条需求")
        table = text[start:].split("\n## ", 1)[0]
        for line in table.splitlines():
            if not line.startswith("| 「"):
                continue
            names = re.findall(r"「([^」]+)」", line.split("|")[1])
            if not any(n in linear for n in names):
                continue
            cell = line.split("|")[2] if line.count("|") > 2 else ""
            if "dB" in cell and "线性" not in cell:
                bad.append((p.name, names, cell.strip()))
    assert not bad, f"认领格与需求相反（需求要线性、格子里写 dB）：{bad}"


def test_no_stale_doc_names():
    stale = ("问答.md", "核心需求.md")
    hits = [(p.name, s) for p in MD for s in stale if s in p.read_text(encoding="utf-8")]
    assert not hits, f"仍在引用已删掉的文件：{hits}"


def test_local_links_exist():
    bad = []
    for p in MD:
        for m in re.finditer(r"\]\(([^)#]+)\)", p.read_text(encoding="utf-8")):
            target = m.group(1).strip()
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (p.parent / target).exists():
                bad.append((p.relative_to(ROOT).as_posix(), target))
    assert not bad, f"链接指向不存在的文件：{bad}"
