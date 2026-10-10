"""历史表「导出原始数据」：离线自检，**不碰硬件、也不碰真的 data/**。

导出的规定动作（错一条就算白导）：

  1. **原文件一个字节都不动** —— 是抄一份出去，不是搬出去。
  2. **表和图对得上**：CSV 一行一个扫描点（没图的点也占一行，并写明为什么），
     文件名就是采集时的名字，尺寸/位深是从**文件自己头上**读出来的。
  3. **口径不许猜**：库与文件两边的裁剪记录对不上就拒绝；设备原值没记下就留空（不是 0）。
  4. **半截不冒充成品**：manifest.json 最后写；zip 先写 .part 再改名；失败不留残骸。

这里造的"PNG"是**只有头的一段字节**（签名 + IHDR + 可选的 tEXt）—— 导出本来就只读这 33 个字节、
不解码任何像素，所以这段字节足够验它，也顺带证明这条路不依赖 PIL / numpy。
直接跑：python backend/tests/test_export.py
"""
from __future__ import annotations

import csv
import io
import json
import struct
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import backend.export as ex  # noqa: E402
from backend import store  # noqa: E402
from backend.stage_api import SETTLE_DEVICE  # noqa: E402

W, H = 1440, 1080


def _chunk(tag: bytes, body: bytes) -> bytes:
    # CRC 照规矩是有的，但读的人（read_png_meta / png_ihdr）不看它，这里填零
    return struct.pack(">I", len(body)) + tag + body + b"\x00\x00\x00\x00"


def _png(w: int = W, h: int = H, bits: int = 16, crop: str = "", pixels: bytes = b"") -> bytes:
    """一张只有头（+可选 tEXt）的 PNG：签名 + IHDR + [tEXt CropRect] + IEND + 像素占位。"""
    out = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, bits, 0, 0, 0, 0))
    if crop:
        out += _chunk(b"tEXt", b"CropRect\x00" + crop.encode("ascii"))
    return out + _chunk(b"IEND", b"") + pixels


@contextmanager
def _workspace(root: Path):
    """库和两个目录都指到临时目录 —— 真的 data/ 一个字节都不碰。"""
    saved = (store.DATA_DIR, store.IMAGE_DIR, store.DB_PATH, ex.DATA_DIR, ex.EXPORT_DIR)
    store.DATA_DIR, store.IMAGE_DIR, store.DB_PATH = root, root / "images", root / "scans.db"
    ex.DATA_DIR, ex.EXPORT_DIR = root, root / "export"
    store.init()
    try:
        yield
    finally:
        (store.DATA_DIR, store.IMAGE_DIR, store.DB_PATH,
         ex.DATA_DIR, ex.EXPORT_DIR) = saved


def _scan(root: Path, images=(0, 1, 2), readback: float | None = 0.75,
          name: str = "导出测试", crop: str = "", db_crop=None) -> int:
    """建一条扫描：images 里的号各写一张图到盘上，其余的点只有元数据没有图。"""
    (root / "images").mkdir(parents=True, exist_ok=True)
    sid = store.create_scan(name, 0.0, 10.0, 3, 300, readback)
    for i in range(3):
        rel = None
        if i in images:
            rel = f"images/scan{sid:04d}_{i:05d}.png"
            (root / rel).write_bytes(_png(crop=crop, pixels=b"x" * (i + 1)))
        store.add_point(sid, i, float(i), 0.75 * i, 100.0, True, SETTLE_DEVICE, rel, 200_000)
    store.finish_scan(sid, "done")
    if db_crop:
        store.put_crops([{"scan_id": sid, "group_name": name, "x0": db_crop[0], "y0": db_crop[1],
                          "w": db_crop[2], "h": db_crop[3], "frames": 3, "saved_bytes": 0,
                          "state": "done", "created_at": 0.0}])
    return sid


def _table(dest: Path) -> list:
    text = (dest / ex.CSV_NAME).read_text("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def test_export_copies_the_frames_and_writes_one_row_per_point():
    """图原样抄走（名字、字节都不变），表里一行一个点，读得动的点带上自己的尺寸与位深。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0, 2))          # 1 号点没有图
            before = {p.name: p.read_bytes() for p in (root / "images").iterdir()}
            seen = []
            out = ex.run(sid, progress=lambda done, total: seen.append((done, total)))
            dest = Path(out["path"])

            assert dest.parent == root / "export" and dest.is_dir(), dest
            assert out["images"] == 2 and out["points"] == 3, out
            assert sorted(p.name for p in dest.iterdir()) == sorted(
                [ex.CSV_NAME, ex.MANIFEST_NAME, *before]), sorted(p.name for p in dest.iterdir())
            for name, raw in before.items():          # 字节级一致
                assert (dest / name).read_bytes() == raw, name
            assert {p.name: p.read_bytes() for p in (root / "images").iterdir()} == before, \
                "源文件不许动"
            assert seen == [(1, 2), (2, 2)], seen     # 进度按张报

            rows = _table(dest)
            assert [r["idx"] for r in rows] == ["0", "1", "2"], rows
            assert rows[1]["image"] == "" and rows[1]["note"] == "没采到图", rows[1]
            assert rows[0]["image"] == f"scan{sid:04d}_00000.png", rows[0]
            assert rows[0]["width"] == str(W) and rows[0]["height"] == str(H), rows[0]
            assert rows[0]["bits"] == "16" and rows[0]["on_target"] == "1", rows[0]
            assert rows[0]["target_um"] == "0.0" and rows[0]["actual_um"] == "0.0", rows[0]
            assert rows[2]["actual_um"] == "1.5" and rows[2]["exposure_us"] == "200000", rows[2]
            assert rows[0]["taken_at_local"] and rows[0]["taken_at"], rows[0]

            man = json.loads((dest / ex.MANIFEST_NAME).read_text("utf-8"))
            assert man["scan"]["id"] == sid and man["points"]["images"] == 2, man
            assert man["crop"] == {"db": None, "file": None, "checked_on": rows[0]["image"]}, man
            assert man["frame"] == {"width": W, "height": H, "bits": 16}, man


def test_raw_readback_column_is_the_device_number_or_empty():
    """设备原值 = 折算值 ÷ 系数；**没记下系数或系数本来就是 1 就留空** —— 不许拿 µm 顶上。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0,), readback=0.75)
            out = ex.run(sid)
            rows = _table(Path(out["path"]))
            assert rows[2]["readback_raw"] == "2.0", rows[2]   # 1.5 µm ÷ 0.75

            for factor in (1.0, None):                        # PI / 老数据（没记设备）
                root2 = root / f"f{factor}"
                root2.mkdir()
                with _workspace(root2):
                    sid2 = _scan(root2, images=(0,), readback=factor)
                    plan = ex.plan(sid2)
                    assert plan["raw_available"] is False, plan
                    rows2 = _table(Path(ex.run(sid2)["path"]))
                    assert rows2[2]["readback_raw"] == "", rows2[2]


def test_missing_file_is_reported_not_skipped():
    """库里记着、盘上没有的图：进 missing 如实上报，表里那一行写明"盘上找不到"。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0, 1, 2))
            (root / f"images/scan{sid:04d}_00001.png").unlink()
            plan = ex.plan(sid)
            assert plan["missing"] == [f"images/scan{sid:04d}_00001.png"], plan
            # 带上 plan 的批次标记：面板上写的就是这个路径，跑完得落在同一处
            rows = _table(Path(ex.run(sid, stamp=plan["stamp"])["path"]))
            assert Path(plan["dest"]).is_dir(), plan["dest"]
            assert rows[1]["image"] == "" and "盘上找不到" in rows[1]["note"], rows[1]
            man = json.loads((Path(plan["dest"]) / ex.MANIFEST_NAME).read_text("utf-8"))
            assert man["points"]["missing_images"] == plan["missing"], man


def test_crop_records_must_agree():
    """库与文件两边的裁剪记录对不上就**拒绝导出**：那是"坐标会悄悄错位"的那一类。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0,), db_crop=(520, 390, 400, 300))   # 库里记了，文件里没写
            for call in (lambda: ex.plan(sid), lambda: ex.run(sid)):
                try:
                    call()
                    raise AssertionError("应该拒绝")
                except ValueError as exc:
                    assert "裁剪记录和文件对不上" in str(exc), exc
            assert not (root / "export").exists(), "拒绝的时候不该留下任何东西"


def test_zip_is_one_file_and_holds_the_same_table():
    """打包是一个文件（.part 写完才改名），包里的表和散图那份内容一致。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0, 1))
            out = ex.run(sid, zipped=True, stamp="20260101-000000")
            dest = Path(out["path"])
            assert dest.name == f"导出测试_{sid}_20260101-000000.zip" and dest.is_file(), dest
            assert [p.name for p in (root / "export").iterdir()] == [dest.name], "不许留 .part"
            with zipfile.ZipFile(dest) as zf:
                names = zf.namelist()
                assert sorted(names) == sorted([ex.CSV_NAME, ex.MANIFEST_NAME,
                                                f"scan{sid:04d}_00000.png",
                                                f"scan{sid:04d}_00001.png"]), names
                assert zf.read(f"scan{sid:04d}_00000.png").startswith(b"\x89PNG"), "原字节"
                rows = list(csv.DictReader(io.StringIO(zf.read(ex.CSV_NAME).decode("utf-8-sig"))))
                assert len(rows) == 3 and rows[2]["image"] == "", rows
                man = json.loads(zf.read(ex.MANIFEST_NAME).decode("utf-8"))
                assert man["files"]["packed"].startswith("zip"), man


def test_a_failed_copy_leaves_nothing_behind():
    """抄不动就整批收摊：不留半份、源文件不动（目标目录是刚从空位挑的，删它安全）。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0, 1))
            bad = root / f"images/scan{sid:04d}_00001.png"
            bad.unlink()
            bad.mkdir()                       # 同名却是个目录：copy2 必然失败
            try:
                ex.run(sid)
                raise AssertionError("应该失败")
            except OSError:
                pass
            assert list((root / "export").iterdir()) == [], "失败不许留残骸"
            assert bad.is_dir() and (root / f"images/scan{sid:04d}_00000.png").exists()


def test_recent_lists_finished_exports_and_marks_half_ones():
    """面板上"上次导哪儿了"：成品和没导完的分得开（没导完 = 没有 manifest.json）。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0,))
            dest = Path(ex.run(sid)["path"])
            (root / "export" / "半截目录").mkdir()
            (root / "export" / "x.zip.part").write_bytes(b"")
            got = {g["name"]: g for g in ex.recent()}
            assert sorted(got) == ["半截目录", dest.name], got
            assert got["半截目录"]["done"] is False, got
            assert got[dest.name]["done"] is True, got
            assert got[dest.name]["images"] == 1 and got[dest.name]["bytes"] > 0, got


def test_name_is_safe_for_windows_and_never_overwrites():
    """名字里不许有进不了文件名的字符；空名字用 scan<号>；目标已经在了就往后编号。"""
    assert ex._safe_name("", 19) == "scan19"
    assert ex._safe_name('a/b\\c:d*e?f"g<h>i|j', 1) == "abcdefghij"
    assert ex._safe_name("名字.", 1) == "名字"
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "一个目录"
        p.mkdir()
        assert ex._free(p).name == "一个目录_2", ex._free(p)
        assert ex._free(Path(d) / "还没有").name == "还没有"
        (Path(d) / "包.zip").write_bytes(b"")
        assert ex._free(Path(d) / "包.zip").name == "包_2.zip"


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except Exception:
            bad += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"{len(fns) - bad}/{len(fns)} 通过")
    sys.exit(1 if bad else 0)
