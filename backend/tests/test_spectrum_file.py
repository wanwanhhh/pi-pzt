"""导入原光谱：认文件、减背景、交数给界面。离线自检，不碰仪器。

要守住的三条：

  1. **认得出两种导出**：光谱仪自家的 xlsx（## 元数据 + A 列为 X 的表头）与两列 CSV；
     波长给 nm 还是 µm 都行 —— 文件里没写单位，只能看量级，这条容易错，所以单独测。
  2. **背景取平段中位数、逐点相减**：减完平段落在 0 附近，峰高 = 原峰 − 背景；
     减不掉或减反了，叠图时两条线的零点对不上、半高宽也会被量宽（实测 7.1 → 8.2 nm）。
  3. **失败说人话**：点太少、没有数、.xls、减完没有峰 —— 都要是一句能看懂的话，不是 KeyError。

直接跑：python backend/tests/test_spectrum_file.py
"""
from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend import spectrum_file  # noqa: E402


def _xlsx(rows: list[list[str]], shared: bool = False) -> bytes:
    """按光谱仪那个形状造一个最小 xlsx：A 列起一行一格。

    shared=True 走共享字符串表（Excel 自己存的就是这样），False 走 inlineStr。
    """
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    table: list[str] = []
    body = []
    for i, row in enumerate(rows, start=1):
        cells = []
        for j, text in enumerate(row):
            col = chr(ord("A") + j)
            if text == "":
                continue
            try:
                float(text)
                cells.append(f'<c r="{col}{i}"><v>{text}</v></c>')
            except ValueError:
                if shared:
                    if text not in table:
                        table.append(text)
                    cells.append(f'<c r="{col}{i}" t="s"><v>{table.index(text)}</v></c>')
                else:
                    cells.append(f'<c r="{col}{i}" t="inlineStr"><is><t>{text}</t></is></c>')
        body.append(f'<row r="{i}">{"".join(cells)}</row>')
    sheet = (f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{ns}">'
             f'<sheetData>{"".join(body)}</sheetData></worksheet>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", sheet)
        if table:
            items = "".join(f"<si><t>{s}</t></si>" for s in table)
            z.writestr("xl/sharedStrings.xml",
                       f'<?xml version="1.0" encoding="UTF-8"?><sst xmlns="{ns}">{items}</sst>')
    return buf.getvalue()


def _fake_spectrum(n: int = 120, base: float = 4000.0, top: float = 30000.0):
    """造一条带平底的谱：峰在 1035 nm、两侧 6 nm 内线性落下，再往外是常数底。"""
    lam, counts = [], []
    for i in range(n):
        nm = 1000.0 + i * 1.0                       # 1000 ~ 1119 nm，1035 正好落在格上
        shape = max(0.0, 1.0 - abs(nm - 1035.0) / 6.0)
        lam.append(nm)
        counts.append(base + (top - base) * shape)
    return lam, counts


def test_xlsx_with_header_row():
    lam, counts = _fake_spectrum()
    rows = [["##name=Sample"], ["##serialNumber=TREI340812"], ["X", "Sample", "参考"]]
    rows += [[f"{x:.4f}", f"{c:.2f}", f"{c:.2f}"] for x, c in zip(lam, counts)]
    out = spectrum_file.parse("1030原光谱.xlsx", _xlsx(rows))

    assert out["origin"] == "列 B「Sample」"
    assert out["n"] == len(lam)
    assert out["background"] == pytest.approx(4000.0)
    assert out["peak_counts"] == pytest.approx(26000.0)
    assert out["raw_peak"] == pytest.approx(30000.0)
    assert out["peak_nm"] == pytest.approx(1035.0, abs=1.0)
    # 平段减完落在 0 上：这正是"叠图零点对得上"的那个前提
    assert out["counts"][0] == pytest.approx(0.0)


def test_shared_strings_sheet():
    """别人拿 Excel 存的 xlsx 走共享字符串表：表头也得认出来。"""
    lam, counts = _fake_spectrum(base=100.0, top=900.0)   # 1035 nm 那个峰得落在范围内
    rows = [["X", "Sample", "说明"]]
    rows += [[f"{x:.3f}", f"{c:.1f}", "d"] for x, c in zip(lam, counts)]
    out = spectrum_file.parse("t.xlsx", _xlsx(rows, shared=True))
    assert out["origin"] == "列 B「Sample」"
    assert out["background"] == pytest.approx(100.0)
    assert out["peak_counts"] == pytest.approx(800.0)


def test_two_column_csv_nm_and_um_agree():
    lam, counts = _fake_spectrum()
    nm = "波长,计数\n" + "".join(f"{x:.3f},{c:.1f}\n" for x, c in zip(lam, counts))
    um = "波长,计数\n" + "".join(f"{x / 1000.0:.6f},{c:.1f}\n" for x, c in zip(lam, counts))
    a = spectrum_file.parse("a.csv", nm.encode("utf-8"))
    b = spectrum_file.parse("b.csv", um.encode("utf-8"))

    assert a["origin"] == b["origin"] == "两列 CSV"
    assert a["peak_nm"] == pytest.approx(b["peak_nm"])
    assert a["peak_counts"] == pytest.approx(b["peak_counts"])
    assert a["peak_nm"] == pytest.approx(1035.0, abs=1.0)


def test_rows_come_out_sorted_by_wavelength():
    """导出工具给的顺序不保证（频率降序那种见过），减背景前必须先按波长排好。"""
    lam, counts = _fake_spectrum()
    text = "\n".join(f"{x:.3f},{c:.1f}" for x, c in reversed(list(zip(lam, counts))))
    out = spectrum_file.parse("rev.csv", text.encode("utf-8"))
    assert out["lam_nm"] == sorted(out["lam_nm"])
    assert out["range_nm"][0] < out["range_nm"][1]


def test_rejects_too_few_points():
    text = "1000,10\n1035,900\n1100,12\n"
    with pytest.raises(ValueError, match="做不了谱"):
        spectrum_file.parse("tiny.csv", text.encode("utf-8"))


def test_rejects_xls_and_garbage():
    with pytest.raises(ValueError, match="另存为 .xlsx"):
        spectrum_file.parse("old.xls", b"whatever")
    with pytest.raises(ValueError, match="打不开这个 xlsx"):
        spectrum_file.parse("broken.xlsx", b"not a zip at all")


def test_rejects_flat_spectrum_after_background():
    """整条都是常数：减完没有峰 —— 不能交一张空图，也不能编一个峰出来。"""
    text = "\n".join(f"{1000 + i},{5000}" for i in range(20))
    with pytest.raises(ValueError, match="找不到峰"):
        spectrum_file.parse("flat.csv", text.encode("utf-8"))
