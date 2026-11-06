"""导入光谱仪导出的谱：认文件 → 减背景 → 交给界面叠图。

页面上的频谱是**前端按像素曲线算出来的**（见 frontend/app.js 的 pixelSpectrum）；
这一条不是 —— 它是另一台仪器拍的，只能把数拿进来，和算出来的那条摆在一起比形状。
所以这里只做三件事：认文件、减背景、把数交出去。

**归一化不在后端**：要让两条线"最大值对齐"，得同时知道导入这条的峰值与当前像素谱的峰值，
后一个只有界面手里有（那是前端算的）。这里把减完背景的原始计数原样交出去，界面自己乘一个系数。

认两种文件（都是这台仪器导得出的东西，实测过）：

- 光谱仪自家的 xlsx：开头几行是 ## 写的元数据，表头行 A 列写 X，B 列起每条谱一列；
- 两列 CSV / TXT：波长,计数。波长列的最大值 < 10 就按 µm 读，否则按 nm ——
  同一个人两种都导出过，文件里没有单位说明，只能看量级。

**为什么要减背景**：光谱仪的平场不是零，整条谱压在一个常数底上（实测 ~4.3k counts）。
不减掉它，叠图时两条线的零点对不上；峰的**宽度**也会被抬高了的半高线量宽（实测 7.1 → 8.2 nm）。
背景取「远离主峰那几段」的中位数：主峰左右有杂散光，均值会被它带偏，中位数只认多数点。

**失败要说人话**：这里抛的 ValueError 会原样走到界面的提示条上 —— 说清是"文件不对"还是
"这条谱里没有峰"，别让人对着一个空图猜。
"""
from __future__ import annotations

import io
import re
import statistics
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

# 少于这么多个点就不画：谱是"一整条曲线"，两三个数连不出来（与前端 SPEC_MIN_POINTS 一个意思）。
MIN_POINTS = 8

# 一份光谱导出撑死几百 KB。8 MB 之外的东西不是它 —— 与其解一个 2 GB 的 xlsx，不如当场拒。
MAX_BYTES = 8 << 20

# 判「远离主峰」的距离（nm）。按波长算而不是按点算：换一台仪器点距不一样。
BG_OFFSET_NM = 10.0

_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_SPLIT = re.compile(r"[,;\t ]+")


def parse(name: str, raw: bytes) -> dict:
    """name 只用来认后缀与在界面上写"哪一列"；raw 是文件原文。"""
    if len(raw) > MAX_BYTES:
        raise ValueError(f"这个文件 {len(raw) / 1e6:.1f} MB，超过 {MAX_BYTES >> 20} MB —— 光谱导出不会有这么大。")
    suffix = Path(name).suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        lam_nm, counts, origin = _read_xlsx(raw)
    elif suffix == ".xls":
        raise ValueError("旧的 .xls 读不了：请在 Excel 里另存为 .xlsx，或导出成两列 CSV。")
    else:
        lam_nm, counts, origin = _read_two_columns(raw)
    return _summarize(name, origin, lam_nm, counts)


def _summarize(name: str, origin: str, lam_nm: list[float], counts: list[float]) -> dict:
    """排序、减背景、把要交给界面的数收成一个字典。"""
    if len(lam_nm) != len(counts):
        raise ValueError("这一列波长和计数一样多才对得上，读出来却不是。")
    if len(lam_nm) < MIN_POINTS:
        raise ValueError(f"只读出 {len(lam_nm)} 个点，做不了谱（至少要 {MIN_POINTS} 个）。")

    pairs = sorted(zip(lam_nm, counts))
    lam_nm = [p[0] for p in pairs]
    counts = [p[1] for p in pairs]

    peak_i = max(range(len(counts)), key=counts.__getitem__)
    peak_nm = lam_nm[peak_i]
    flat = [c for x, c in pairs if abs(x - peak_nm) > BG_OFFSET_NM]
    if len(flat) < MIN_POINTS:
        # 整条谱都在主峰 ±10 nm 以内：没有平段可估背景，就按背景 = 0 交出去，别编一个数。
        background = 0.0
    else:
        background = float(statistics.median(flat))

    after = [c - background for c in counts]
    peak_after = max(after)
    if not (peak_after > 0):
        raise ValueError("减完背景这条谱整个不高于零 —— 找不到峰，也许背景估反了或文件里不是谱。")

    # 小数位够用就行：JSON 里 2048 个点 ×2 列，多写几位只是让传输变慢
    return {
        "name": name,
        "origin": origin,
        "n": len(lam_nm),
        "lam_nm": [round(x, 4) for x in lam_nm],
        "counts": [round(c, 3) for c in after],
        "background": round(background, 2),
        "peak_nm": round(peak_nm, 4),
        "peak_counts": round(peak_after, 2),
        "raw_peak": round(counts[peak_i], 2),
        "range_nm": [round(lam_nm[0], 3), round(lam_nm[-1], 3)],
    }


def _read_xlsx(raw: bytes) -> tuple[list[float], list[float], str]:
    try:
        books = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ValueError("打不开这个 xlsx（连压缩包都不是）—— 另存为 .xlsx，或导出成两列 CSV。") from exc

    names = books.namelist()
    shared = _shared_strings(books, names)
    rows = _sheet_rows(_first_sheet(books, names), shared)

    header = next((i for i, r in enumerate(rows) if r.get("A") == "X"), None)
    if header is not None:
        # 光谱仪自家格式：表头行点名列列，取第一条谱（B 列）
        body = [r for r in rows[header + 1:] if _num(r.get("A")) is not None]
        lam = [float(r["A"]) for r in body]
        counts = [float(r["B"]) for r in body if _num(r.get("B")) is not None]
        if len(lam) != len(counts):
            raise ValueError("表头写着 X，可 B 列有空格子 —— 这条谱缺数，不减背景硬画就是编数。")
        return _to_nm(lam), counts, f"列 B「{rows[header].get('B', 'B')}」"
    # 没有表头：头两列就是数字（别人拿 Excel 存的两列表也不少见）
    pairs = [(float(r["A"]), float(r["B"])) for r in rows
             if _num(r.get("A")) is not None and _num(r.get("B")) is not None]
    if not pairs:
        raise ValueError("这个 xlsx 里没找到数字：既没有 A 列为 X 的表头，也没有两列数。")
    return _to_nm([p[0] for p in pairs]), [p[1] for p in pairs], "A/B 两列数字"


def _read_two_columns(raw: bytes) -> tuple[list[float], list[float], str]:
    text = raw.decode("utf-8-sig", errors="replace")
    pairs: list[tuple[float, float]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = _SPLIT.split(line)
        if len(parts) < 2:
            continue
        a, b = _num(parts[0]), _num(parts[1])
        if a is None or b is None:      # 表头行（"波长,计数"）走到这里被跳过
            continue
        pairs.append((a, b))
    if not pairs:
        raise ValueError("这个文件里没读出两列数 —— 要的是「波长, 计数」两列，表头行可有可无。")
    return _to_nm([p[0] for p in pairs]), [p[1] for p in pairs], "两列 CSV"


def _to_nm(lam: list[float]) -> list[float]:
    """波长列给的是 µm 还是 nm，看量级：一条谱不会短于 10 nm，也不会长于 10 µm。"""
    return [x * 1000.0 for x in lam] if max(lam) < 10 else list(lam)


def _num(text: str | None) -> float | None:
    """这一格是不是一个数（空、文字、表头都算不是）。"""
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value == value else None      # NaN 也不是数


def _shared_strings(books: zipfile.ZipFile, names: list[str]) -> list[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    root = ET.fromstring(books.read("xl/sharedStrings.xml"))
    return ["".join(t.text or "" for t in si.iter(_XLSX_NS + "t")) for si in root.findall(_XLSX_NS + "si")]


def _first_sheet(books: zipfile.ZipFile, names: list[str]) -> ET.Element:
    """取第一张表：导出工具一律写 sheet1.xml；不是的话按名字排第一个。"""
    path = "xl/worksheets/sheet1.xml"
    if path not in names:
        sheets = sorted(n for n in names if n.startswith("xl/worksheets/") and n.endswith(".xml"))
        if not sheets:
            raise ValueError("这个 xlsx 里没有工作表。")
        path = sheets[0]
    return ET.fromstring(books.read(path))


def _sheet_rows(sheet: ET.Element, shared: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for row in sheet.iter(_XLSX_NS + "row"):
        cells: dict[str, str] = {}
        for c in row.findall(_XLSX_NS + "c"):
            ref = c.get("r") or ""
            col = "".join(ch for ch in ref if ch.isalpha())
            value = c.find(_XLSX_NS + "v")
            text = value.text if value is not None else ""
            if c.get("t") == "s" and text:
                text = shared[int(text)]
            elif c.get("t") == "inlineStr":
                text = "".join(t.text or "" for t in c.iter(_XLSX_NS + "t"))
            cells[col] = text
        rows.append(cells)
    return rows
