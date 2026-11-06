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


def test_a_bad_number_in_the_table_is_refused_before_anything_is_copied():
    """表里某一格不是数字：**预检就拒**，盘上一张图都不留，改好之后同一包还能导。

    实测过的样子：target_um 改成 'abc' → 以前是图都就位了才在写库那一步抛 ValueError，
    盘上留两张没人认领的图、库里没有这一条；再导被"盘上已经有 2 张同名的图"挡住。
    """
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0, 1))
            pack = _pack(root, sid)
            _drop_scan(sid)
            good = _rows(pack)
            rows = [dict(r) for r in good]
            rows[1]["target_um"] = "abc"
            _write_rows(pack, rows)

            _expect_refused(pack, "target_um")
            assert store.get_scan(sid) is None
            assert not list((root / "images").glob("*.png")), "预检拒掉时一张图都不许抄进来"
            assert not list(root.glob(".import-*")), "暂存目录要收拾干净"

            _write_rows(pack, good)                  # 把那一格改回去：同一包能重导
            out = im.run(pack)
            assert out["scan_id"] == sid and out["images"] == 2, out
            assert len(list((root / "images").glob("*.png"))) == 2
            assert store.get_scan(sid) is not None


def test_a_manifest_scan_block_missing_optional_keys_still_imports():
    """清单 scan 一栏缺了"缺了按空写"的那几格（name / status / message / …）：照导，不炸。

    以前 _SCAN_KEYS 把 message 也算成"一个都不能少"，可预检查的是别的键 —— 于是清单缺
    message 时预检放行、写库那一步 ProgrammingError，图已经就位了。
    """
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0,))
            pack = _pack(root, sid)
            _drop_scan(sid)
            man = _manifest(pack)
            for key in ("message", "name", "status", "finished_at", "readback_to_um"):
                man["scan"].pop(key)
            _write_manifest(pack, man)

            out = im.run(pack)
            assert out["scan_id"] == sid, out
            got = store.get_scan(sid)
            assert got is not None and got["message"] == "" and got["name"] == "", got
            assert got["readback_to_um"] is None, "没记下口径就是没有，不猜一个系数"


def test_two_rows_pointing_at_the_same_frame_are_refused():
    """表里两行指着同一张图：就位时第二张会 FileNotFoundError，图已经挪过去一半 —— 预检拒。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0, 1))
            pack = _pack(root, sid)
            _drop_scan(sid)
            rows = _rows(pack)
            rows[1]["image"] = rows[0]["image"]
            rows[1]["image_bytes"] = rows[0]["image_bytes"]
            _write_rows(pack, rows)
            _expect_refused(pack, "同一张图")
            assert not list((root / "images").glob("*.png")), "一张图都不许抄进来"


def test_a_failure_after_the_frames_are_in_place_takes_them_away():
    """就位之后任何失败：删掉**本次刚挪过去**的那几张，不留没人认领的图。

    这是"库里没有、盘上有"那一档：界面上看不见它们，同一包从此再也导不进来。
    写库那一步现在是纯 INSERT（值在预检里就解析完了），所以这里把它逼出来验回收。
    """
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0, 1))
            pack = _pack(root, sid)
            _drop_scan(sid)
            saved = store.import_scan

            def boom(*_args, **_kw):
                raise RuntimeError("写库这一步炸了")

            store.import_scan = boom
            try:
                try:
                    im.run(pack)
                except RuntimeError as exc:
                    assert "炸了" in str(exc), exc
                else:
                    raise AssertionError("写库失败却没抛出来")
            finally:
                store.import_scan = saved
            assert store.get_scan(sid) is None
            assert not list((root / "images").glob("*.png")), "刚挪过去的图要删掉"
            assert not list(root.glob(".import-*")), "暂存目录要收拾干净"
            assert im.run(pack)["scan_id"] == sid, "收拾干净之后同一包还能导"


def test_a_frame_that_was_missing_on_disk_keeps_its_path_across_two_round_trips():
    """「库里记着、盘上没有」的图：往返一次路径不许丢，再导一次两张表逐行相同。

    实测过的样子：三点的扫描删掉 1 号图再导出，导回来 image_path 成了 [名字, None, 名字]，
    再导一次 note 变成「没采到图」、missing_images 变空 —— 那条路径永久没了。
    """
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0, 1, 2))
            (root / f"images/scan{sid:04d}_00001.png").unlink()
            before = _snapshot(sid)
            assert before["points"][1]["image_path"] == f"images/scan{sid:04d}_00001.png"

            pack = _pack(root, sid, stamp="20260101-000000")
            first_rows = _rows(pack)
            first_man = _manifest(pack)
            _drop_scan(sid)
            im.run(pack)

            assert _snapshot(sid) == before, _snapshot(sid)["points"]
            assert not (root / "images" / f"scan{sid:04d}_00001.png").exists(), \
                "包里没有这一张，导进来也不该凭空多一张"

            again = _pack(root, sid, stamp="20260101-000001")
            assert _rows(again) == first_rows, "再导一次，表要逐行相同"
            second_man = _manifest(again)
            for key in ("points", "scan", "crop", "frame", "files", "notes", "format", "kind"):
                assert second_man[key] == first_man[key], (key, second_man[key], first_man[key])


def test_a_manifest_that_says_missing_and_the_pack_has_it_is_refused():
    """清单说这张缺、包里却装着一张：这包自己前后矛盾，不收（不猜哪一边算数）。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0, 1))
            pack = _pack(root, sid)
            _drop_scan(sid)
            man = _manifest(pack)
            man["points"]["missing_images"] = [f"images/scan{sid:04d}_00000.png"]
            _write_manifest(pack, man)
            _expect_refused(pack, "对不上")


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


def test_an_import_that_races_a_new_scan_cannot_take_its_number():
    """导入与开扫描并发抢号：号是**抄图之前**就占住的，抢号的那条只能拿下一个号。

    实测过的样子：预检那一刻号是空的，抄图这几秒里 store.create_scan 正好把那个号发出去，
    导入随后 os.replace 盖掉**正在采的那张图**、最后撞主键。这里在抄第一张图的时候
    插一次 create_scan，把那个竞态摆出来。
    """
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0, 1))
            pack = _pack(root, sid)
            _drop_scan(sid)
            taken: list = []

            def racing(done, total):                  # 抄图途中：扫描那边来要号了
                if taken or done < 1:
                    return
                new_sid = store.create_scan("正在采的那条", 0.0, 1.0, 2, 100, 1.0)
                # 那条扫描采下的第一张图：导入不许动它
                (root / "images" / f"scan{new_sid:04d}_00000.png").write_bytes(b"\x89PNG running")
                taken.append(new_sid)

            out = im.run(pack, progress=racing)
            assert taken, "这条测试要真的插一次抢号"
            assert out["scan_id"] == sid, out
            assert taken[0] != sid, "导入占住的号不许被发出去"
            # 抢号那条的图一个字节没动
            assert (root / "images" / f"scan{taken[0]:04d}_00000.png").read_bytes() == \
                b"\x89PNG running"
            # 导入那条也在：3 个点、2 张图
            assert store.get_scan(sid)["status"] == "done", store.get_scan(sid)
            assert len(store.get_points(sid)) == 3
            assert store.get_scan(taken[0])["status"] == "running", store.get_scan(taken[0])


def test_an_import_that_loses_the_number_race_fails_cleanly():
    """号在"预检 → 占号"之间被扫描抢走：导入干净失败（无行、无孤儿图），抢号那条的图不动。

    这是那条竞态的另一半：**占号**本身没成。这时一张图都还没抄，什么都不该留下。
    """
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0, 1))
            pack = _pack(root, sid)
            _drop_scan(sid)
            # 本机一条扫描都没有：号池归零，扫描那边新开一条就会拿到**同一个号**
            with store._db() as conn:
                conn.execute("DELETE FROM sqlite_sequence WHERE name = 'scan'")
            (root / "images").mkdir(parents=True, exist_ok=True)
            saved = store.reserve_scan

            def raced(scan_row):
                # 预检放行之后、占号之前那一段：扫描那边正好把这个号发出去，还采下了第一张图
                new_sid = store.create_scan("正在采的那条", 0.0, 1.0, 2, 100, 1.0)
                (root / "images" / f"scan{new_sid:04d}_00000.png").write_bytes(b"\x89PNG running")
                return saved(scan_row)

            store.reserve_scan = raced
            try:
                im.run(pack)
            except ValueError as exc:
                assert f"#{sid}" in str(exc), exc
            else:
                raise AssertionError("号被占了，导入本该当场拒")
            finally:
                store.reserve_scan = saved

            assert store.importing_scans() == [], "不许留一行 importing"
            assert store.list_scans()[0]["status"] == "running", store.list_scans()
            # 抢号那条的图一个字节没动；导入一张都没落
            assert sorted(p.name for p in (root / "images").iterdir()) == [
                f"scan{sid:04d}_00000.png"]
            assert (root / "images" / f"scan{sid:04d}_00000.png").read_bytes() == b"\x89PNG running"
            assert not list(root.glob(".import-*")), "暂存目录要收拾干净"


def test_an_import_that_fails_after_reserving_drops_the_row_and_the_frames():
    """占了号之后失败：那一行与已经挪过去的图都要撤掉 —— 别留一行 importing 混进历史表。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            sid = _scan(root, images=(0, 1))
            pack = _pack(root, sid)
            _drop_scan(sid)
            saved = store.import_scan

            def boom(*_a, **_kw):
                raise RuntimeError("写点这一步炸了")

            store.import_scan = boom
            try:
                try:
                    im.run(pack)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("本该失败")
            finally:
                store.import_scan = saved
            assert store.get_scan(sid) is None, "占号的那一行要撤掉"
            assert store.importing_scans() == [], "不许留一行 importing"
            assert not list((root / "images").glob("*.png"))
            assert im.run(pack)["scan_id"] == sid


def test_sweep_clears_an_import_that_was_killed_halfway():
    """进程被杀在导入半路：占着号的 scan 行、它已经落盘的图、暂存目录一起清掉。

    那一行**不是一次扫描**（一个点都没采过）：留着会在历史表里冒充一条扫描。
    init() 也不许把它改写成 aborted（那样就真成了一条"中断的扫描"）。
    """
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with _workspace(root):
            row = {"id": 79, "name": "半路", "start_um": 0.0, "stop_um": 1.0, "count": 3,
                   "settle_ms": 100, "status": "done", "message": "", "created_at": 0.0,
                   "finished_at": None, "readback_to_um": 1.0}
            store.reserve_scan(row)
            (root / "images").mkdir(parents=True, exist_ok=True)
            (root / "images" / "scan0079_00000.png").write_bytes(_png())
            (root / "images" / "scan0079_00001.png").write_bytes(_png())
            leftover = root / f"{im.STAGE_PREFIX}79-20260101-000000"
            leftover.mkdir(parents=True)
            # 就位日志：前两张已经挪过去了（崩在第三张之前）
            (leftover / im.PLACED_NAME).write_text("scan0079_00000.png\nscan0079_00001.png\n",
                                                   "utf-8")
            (leftover / "scan0079_00002.png").write_bytes(b"half a copy")

            store.init()                      # 启动：不许把 importing 改写成 aborted
            got = store.get_scan(79)
            assert got is not None and got["status"] == store.IMPORTING, got

            assert im.sweep() == 2            # 一行 + 一个暂存目录
            assert store.get_scan(79) is None, "占号的那一行要清掉"
            assert store.list_scans() == [], "历史表里不许留下它"
            assert not list((root / "images").glob("*.png")), "它已经就位的图也要清掉"
            assert not leftover.exists()


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
