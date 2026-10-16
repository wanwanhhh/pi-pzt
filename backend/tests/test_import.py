"""历史表「导入数据」：离线自检，**不碰硬件、也不碰真的 data/**。

导入的规定动作（错一条就算白导）：

  1. **只认导完了的包** —— 清单不在（导出最后一步才写它）就是没写完，拒收。
  2. **源包一个字节都不动**，本机不覆盖任何东西：号被占了、图名撞了，都在预检里拦下。
  3. **值全部照抄**：命令值、读数、曝光、采图时刻、裁剪矩形，一个都不重算
     （saved_bytes 例外，它答的是"**这台机器上**省了多少"，搬进来的记录没有这个数，
     留空 —— 不拿 0 顶）。
  4. **半截不算数**：图先落暂存、逐张核对过了才就位，最后才写库；失败不留库记录、不留半张图。

包是用**导出那一侧**现造的（往返一起验）：造一条扫描 → 导出 → 删掉 → 导入 → 逐字段对。
直接跑：python backend/tests/test_import.py
"""
from __future__ import annotations

import csv
import io
import json
import struct
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import backend.export as ex  # noqa: E402
import backend.importer as im  # noqa: E402
import backend.package as pkg  # noqa: E402
from backend import store  # noqa: E402
from backend.stage_api import SETTLE_DEVICE  # noqa: E402

W, H = 1440, 1080


def _chunk(tag: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + tag + body + b"\x00\x00\x00\x00"


def _png(w: int = W, h: int = H, bits: int = 16, crop: str = "", pixels: bytes = b"") -> bytes:
    """一张只有头（+可选 tEXt）的 PNG —— 与导出那份测试同一个做法。"""
    out = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, bits, 0, 0, 0, 0))
    if crop:
        out += _chunk(b"tEXt", b"CropRect\x00" + crop.encode("ascii"))
    return out + _chunk(b"IEND", b"") + pixels


@contextmanager
def _workspace(root: Path):
    """库与两个目录都指到临时目录 —— 真的 data/ 一个字节都不碰。"""
    saved = (store.DATA_DIR, store.IMAGE_DIR, store.DB_PATH, ex.DATA_DIR, ex.EXPORT_DIR,
             im.DATA_DIR, im.IMAGE_DIR)
    store.DATA_DIR, store.IMAGE_DIR, store.DB_PATH = root, root / "images", root / "scans.db"
    ex.DATA_DIR, ex.EXPORT_DIR = root, root / "export"
    im.DATA_DIR, im.IMAGE_DIR = root, root / "images"
    store.init()
    try:
        yield
    finally:
        (store.DATA_DIR, store.IMAGE_DIR, store.DB_PATH, ex.DATA_DIR, ex.EXPORT_DIR,
         im.DATA_DIR, im.IMAGE_DIR) = saved


def _scan(root: Path, images=(0, 1, 2), readback: float | None = 0.75,
          name: str = "导入测试", crop: str = "", db_crop=None, sid: int | None = None) -> int:
    """建一条扫描：images 里的号各写一张图到盘上，其余的点只有元数据没有图。"""
    (root / "images").mkdir(parents=True, exist_ok=True)
    sid = store.create_scan(name, 0.0, 10.0, 3, 300, readback)
    for i in range(3):
        rel = None
        if i in images:
            rel = f"images/scan{sid:04d}_{i:05d}.png"
            (root / rel).write_bytes(_png(crop=crop, pixels=b"x" * (i + 1)))
        store.add_point(sid, i, float(i), 0.75 * i, 100.0 + i, True, SETTLE_DEVICE, rel, 200_000)
    store.finish_scan(sid, "done")
    if db_crop:
        store.put_crops([{"scan_id": sid, "x0": db_crop[0], "y0": db_crop[1],
                          "w": db_crop[2], "h": db_crop[3], "frames": 3, "saved_bytes": 1234,
                          "state": "done", "created_at": 0.0}])
    return sid


def _pack(root: Path, sid: int, **kw) -> Path:
    """导出一份，返回包的路径。"""
    return Path(ex.run(sid, **kw)["path"])


def _rows(pack: Path) -> list:
    return list(pkg.read_csv((pack / pkg.CSV_NAME).read_bytes()))


def _write_rows(pack: Path, rows: list) -> None:
    (pack / pkg.CSV_NAME).write_bytes(pkg.csv_bytes(rows))


def _manifest(pack: Path) -> dict:
    return json.loads((pack / pkg.MANIFEST_NAME).read_text("utf-8"))


def _write_manifest(pack: Path, man: dict) -> None:
    (pack / pkg.MANIFEST_NAME).write_text(json.dumps(man, ensure_ascii=False), "utf-8")


def _drop_scan(sid: int) -> None:
    """把这条扫描连图带记录删干净 —— 好腾出号来试导入。"""
    store.delete_scan(sid)


def _snapshot(sid: int) -> dict:
    scan = store.get_scan(sid)
    pts = [dict(p) for p in store.get_points(sid)]
    return {"scan": scan, "points": pts}


def test_round_trip_restores_the_scan_exactly():
    """导出 → 删掉 → 导入：扫描行、每个点、裁剪记录、图本身，逐条与原来一样。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0, 2), crop="200,100,1000,700", db_crop=(200, 100, 1000, 700))
            before = _snapshot(sid)
            frames = {p.name: p.read_bytes() for p in (root / "images").iterdir()}
            pack = _pack(root, sid)
            _drop_scan(sid)
            assert store.get_scan(sid) is None

            seen = []
            out = im.run(pack, progress=lambda done, total: seen.append((done, total)))

            assert out["scan_id"] == sid and out["points"] == 3 and out["images"] == 2, out
            after = _snapshot(sid)
            assert after["scan"] == before["scan"], (after["scan"], before["scan"])
            assert after["points"] == before["points"], after["points"]
            # 图按原名字、原字节回到盘上；源包还在原地没动
            assert {p.name: p.read_bytes() for p in (root / "images").iterdir()} == frames
            assert {p.name for p in pack.iterdir()} >= set(frames), "源包不许动"
            assert seen[0][0] == 1 and seen[-1][0] >= 2, seen   # 前半抄、后半核对

            crop = store.get_crop(sid)
            assert (crop["x0"], crop["y0"], crop["w"], crop["h"]) == (200, 100, 1000, 700), crop
            assert crop["state"] == "done" and crop["frames"] == 2, crop
            # 那个数答的是"**这台机器上**省了多少"：本机没量过就留空，
            # 源记录里的 1234 不跟包过来、也不拿 0 顶
            assert crop["saved_bytes"] is None, crop["saved_bytes"]
            # 没图的点照旧没图（不是"补一张"）
            assert [p["image_path"] for p in after["points"]][1] is None, after["points"]
            assert not list(root.glob(".import-*")), "暂存目录要收拾干净"


def test_points_table_keeps_the_device_numbers():
    """表里的读数值照抄进库：actual_um 是采图那一刻的读数，不是命令值。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0,))
            pack = _pack(root, sid)
            _drop_scan(sid)
            im.run(pack)
            pts = store.get_points(sid)
            assert [p["target_um"] for p in pts] == [0.0, 1.0, 2.0], pts
            assert [p["actual_um"] for p in pts] == [0.0, 0.75, 1.5], pts
            assert pts[0]["exposure_us"] == 200_000 and pts[0]["on_target"] == 1, pts[0]
            assert pts[0]["settle_source"] == SETTLE_DEVICE, pts[0]
            assert pts[0]["taken_at"] > 0, pts[0]


def test_a_package_without_a_manifest_is_refused():
    """清单是导出最后一步写的：它不在就是没导完，不收（半截不算数）。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0,))
            pack = _pack(root, sid)
            _drop_scan(sid)
            (pack / pkg.MANIFEST_NAME).unlink()
            _expect_refused(pack, "清单")
            assert store.get_scan(sid) is None


def test_an_old_package_without_a_format_number_still_reads():
    """加版本号之前导的包（清单里没有 format）照旧能读 —— 它们还躺在一堆硬盘上。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0,))
            pack = _pack(root, sid)
            _drop_scan(sid)
            man = _manifest(pack)
            man.pop("format")
            _write_manifest(pack, man)
            out = im.run(pack)
            assert out["scan_id"] == sid, out
            assert store.get_scan(sid) is not None


def test_a_newer_format_is_refused_instead_of_guessed():
    """版本比本机新：说清、别猜（按旧格式读新包就是静默丢东西）。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0,))
            pack = _pack(root, sid)
            _drop_scan(sid)
            man = _manifest(pack)
            man["format"] = pkg.FORMAT + 1
            _write_manifest(pack, man)
            _expect_refused(pack, "格式")


def test_an_occupied_scan_id_is_refused():
    """本机已经有这个号：不覆盖任何东西，也不偷偷换个号（图名就是照这个号起的）。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0,))
            pack = _pack(root, sid)
            _expect_refused(pack, f"#{sid}")


def test_an_existing_frame_is_refused():
    """盘上已经有同名的图（库里却没有这条扫描）：停下来说清，别覆盖。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0,))
            pack = _pack(root, sid)
            name = _rows(pack)[0]["image"]
            _drop_scan(sid)                      # 图删了……
            (root / "images" / name).write_bytes(_png())    # ……又有人写了一张同名的
            _expect_refused(pack, name)
            assert store.get_scan(sid) is None


def test_a_frame_name_with_a_path_is_refused():
    """表是从外面来的：图名带路径（../../x.png）就会写到包外面去，当场拒。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0,))
            pack = _pack(root, sid)
            _drop_scan(sid)
            rows = _rows(pack)
            rows[0]["image"] = "../../逃出去.png"
            _write_rows(pack, rows)
            _expect_refused(pack, "不合法")


def test_a_half_copied_package_is_refused():
    """清单没声明缺、包里却少一张：这包拷了一半，整包拒收。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0, 1))
            pack = _pack(root, sid)
            _drop_scan(sid)
            (pack / _rows(pack)[0]["image"]).unlink()
            _expect_refused(pack, "一半")
            assert store.get_scan(sid) is None


def test_a_frame_that_does_not_match_the_table_leaves_nothing():
    """落盘后核对不过（尺寸与表里写的不一样）：不写库、不留图、不留暂存目录。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0,))
            pack = _pack(root, sid)
            name = _rows(pack)[0]["image"]
            raw = (pack / name).read_bytes()
            # 同字节数、不同尺寸（像素占位一样长，只有 IHDR 里写着的那两个数变了）：
            # 抄写这一关过得去，核对这一关必须拦住
            (pack / name).write_bytes(_png(w=W // 2, h=H // 2, pixels=b"x"))
            assert len((pack / name).read_bytes()) == len(raw), "长度要一样"
            _drop_scan(sid)
            _expect_run_refused(pack, "尺寸")
            assert store.get_scan(sid) is None
            assert not list((root / "images").glob("*.png")), "半张图都不许留在盘上"
            assert not list(root.glob(".import-*")), "暂存目录要收拾干净"


def test_crop_records_that_disagree_are_refused():
    """清单里库与文件两份裁剪记录对不上：原点错了不会报错，只能在这儿拦住。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0,), crop="200,100,1000,700", db_crop=(200, 100, 1000, 700))
            pack = _pack(root, sid)
            _drop_scan(sid)
            man = _manifest(pack)
            man["crop"]["file"] = [0, 0, 1000, 700]
            _write_manifest(pack, man)
            _expect_refused(pack, "裁剪记录")


def test_a_crop_rect_on_the_frame_that_disagrees_is_refused():
    """落盘后对着图再核一次裁剪矩形：清单自己一致、图上却是另一块，也停。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0,), crop="200,100,1000,700", db_crop=(200, 100, 1000, 700))
            pack = _pack(root, sid)
            name = _rows(pack)[0]["image"]
            _drop_scan(sid)
            # tEXt 里的矩形换一块（字符数一样，所以字节数不变 —— 尺寸、字节数两关都过，
            # 一路走到"拿图上的矩形对清单"那一关）
            (pack / name).write_bytes(_png(crop="200,300,1000,700", pixels=b"x"))
            assert (pack / name).stat().st_size == int(_rows(pack)[0]["image_bytes"])
            _expect_run_refused(pack, "矩形")
            assert store.get_scan(sid) is None


def test_a_zip_package_imports_the_same_way():
    """zip 与目录两条路同一个结果（zip 里的图另过一遍 CRC，见 zipfile）。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0, 2))
            before = _snapshot(sid)
            frames = {p.name: p.read_bytes() for p in (root / "images").iterdir()}
            pack = _pack(root, sid, zipped=True, stamp="20260101-000000")
            assert pack.suffix == ".zip", pack
            _drop_scan(sid)
            out = im.run(pack)
            assert out["zipped"] is True and out["images"] == 2, out
            assert _snapshot(sid) == before
            assert {p.name: p.read_bytes() for p in (root / "images").iterdir()} == frames


def test_sweep_clears_a_leftover_stage_dir():
    """进程被杀留下的暂存目录：启动时清掉（源包不动，盘上只是多余的副本）。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            leftover = root / f"{im.STAGE_PREFIX}79-20260101-000000"
            leftover.mkdir(parents=True)
            (leftover / "scan0079_00000.png").write_bytes(b"half a copy")
            assert im.sweep() == 1
            assert not leftover.exists()


def _expect_run_refused(pack, needle: str) -> None:
    """预检**看不出来**的（它不读图）留给落盘后的核对 —— 那时才拒，话里一样说清。

    这两条正是"预检只读两张表"这个分工的代价：要等抄完才对得上，所以拒之前会白抄一遍。
    换来的是 zip 里的图不解开也能先给预检结果（见 importer 的 _verify_staged）。
    """
    im.plan(pack)                        # 预检放行（它没读图）
    try:
        im.run(pack)
    except (ValueError, OSError) as exc:
        assert needle in str(exc), (needle, str(exc))
    else:
        raise AssertionError(f"本该被拒（话里要有「{needle}」）：{pack}")


def _expect_refused(pack, needle: str) -> None:
    """预检要拦在**动手之前**，并且话里说得清是哪儿不对。"""
    for call in (lambda: im.plan(pack), lambda: im.run(pack)):
        try:
            call()
        except ValueError as exc:
            assert needle in str(exc), (needle, str(exc))
        else:
            raise AssertionError(f"本该被拒（话里要有「{needle}」）：{pack}")


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            fn()
        except Exception as exc:                      # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok    {name}")
    print(f"\n{len([n for n in globals() if n.startswith('test_')]) - failed}"
          f"/{len([n for n in globals() if n.startswith('test_')])} 通过")
    sys.exit(1 if failed else 0)
