"""导出：把一条扫描的**原始帧**与**每点元数据**抄一份到 data/export/ 下面。

**是"抄一份出去"，不是"搬出去"**：源文件一个字节都不动，不碰设备、也不排队进设备线程 ——
跟数据页取数同一类，所以扫描跑着也能导；代价只是读盘会跟采图抢带宽（界面上先提醒一句）。

三条口径写在这里，别处不许再写一遍：

  * **图是原样的**：整字节复制，不解码、不裁剪、不改名（用的还是库里 image_path 的那个名字）。
    打包也用 ZIP_STORED —— PNG 已经压过，再 deflate 一遍省不到东西，只是白花 CPU。
  * **坐标一直是原始坐标**：库里的 crop 记录与 PNG 自己 tEXt 里的 CropRect 都写进清单，
    对不上就**当场拒绝** —— 原点偏移错了不会报错，只会让整条曲线悄悄错位，这是唯一能发现它的地方。
  * **缺什么写什么**：没图的点、库里记着而盘上没有的图、读不动头的图，都在 CSV 与清单里
    如实写出来，不补值、不跳过、不当成 0。

导完了的标志：目录模式下是 manifest.json（最后一步才写）；zip 模式是先写 .part、
整包写完才改名 —— 半截产物不冒充成品。失败时把这次新造的目录 / 半截包删掉（删的都是自己刚写的，
源文件一个字节没动）。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import shutil
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import store
from .config import DATA_DIR, EXPORT_DIR, raw_readback_factor

log = logging.getLogger(__name__)

CSV_NAME = "points.csv"
MANIFEST_NAME = "manifest.json"

# 每点一行。列名用 ASCII：这张表是要拿出去给别的软件读的（Excel / Python / MATLAB），
# 中文列名一路要过编码的关。每一列什么意思写在清单的 notes 里，也写在界面的导出面板上。
CSV_COLUMNS = (
    "idx",              # 扫描点序号（0 起，与库里一致）
    "target_um",        # 命令值：想让台子去哪（µm）
    "actual_um",        # 采图那一刻的读出位置（µm）——**不是**命令值，别混用
    "readback_raw",     # 同一串位置的**设备原值**（= actual_um ÷ readback_to_um）；没记下系数就是空的
    "settle_ms",        # 从到位到采图等了多久
    "on_target",        # 读数与命令值的比较（1/0）；设备不拿它做判据
    "settle_source",    # 这个"到位"是谁给的（device / software / unknown）
    "exposure_us",      # 这一点采图用的曝光（相机读回值）；未记录就空着
    "taken_at",         # 采图时刻（Unix 秒，当时那台机器的钟）
    "taken_at_local",   # 同一时刻的本地时间（带时区偏移）
    "image",            # 这一点的图在导出物里的文件名；空 = 没有
    "image_bytes",
    "width", "height", "bits",   # 图自己头上写的尺寸与位深（16 位原值 / 8 位占位图要分得清）
    "note",             # 空 = 正常；否则写清为什么这一格是空的
)


def _stamp() -> str:
    """导出的批次标记：一批一个名字，同名的两批不会撞在一起。"""
    return time.strftime("%Y%m%d-%H%M%S")


def _safe_name(name: str, scan_id: int) -> str:
    """目标名里那一段人看得懂的名字。

    只留**能进文件名**的字符（Windows 不许 \\ / : * ? " < > |），空名字（有 19 条老扫描没名字）
    就用 scan<号> —— 目标名里另有扫描号，撞不了。末尾的点与空格也去掉：Windows 上这样的
    名字存不下（资源管理器会把它悄悄改掉，改名之后就对不上了）。
    """
    out = "".join(c for c in (name or "") if c.isprintable() and c not in '\\/:*?"<>|')
    out = out.strip().strip(".")
    return (out or f"scan{scan_id}")[:40]


def _dest(scan: dict, stamp: str, zipped: bool) -> Path:
    """这次导出落在哪儿：data/export/<名字>_<扫描号>_<批次>[.zip]。"""
    base = f"{_safe_name(scan['name'], scan['id'])}_{scan['id']}_{stamp}"
    return EXPORT_DIR / (base + ".zip" if zipped else base)


def _free(path: Path) -> Path:
    """目标已经在了就往后编号 —— **不覆盖任何东西**（上一次导的可能正躺在那儿）。"""
    if not path.exists():
        return path
    for n in range(2, 1000):
        cand = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not cand.exists():
            return cand
    raise ValueError(f"{path.name} 这个名字下已经排了一长串，先清一清 {path.parent}")


def _collect(scan_id: int) -> tuple:
    """这条扫描现在有什么可导：扫描行、全部点、[(点, 源文件)]、找不到的图、总字节。

    源文件**只按库里的 image_path 找**（不按文件名前缀去猜）：库里记着才算数。
    库里记着而盘上没有的，进 missing 如实上报，不静默跳过。
    """
    scan = store.get_scan(scan_id)
    if scan is None:
        raise ValueError(f"扫描 #{scan_id} 不存在")
    rec = store.get_crop(scan_id)
    if rec and rec["state"] != "done":
        raise ValueError("这条扫描正在裁剪，等它跑完再导（那些文件此刻可能是缺的）")
    points = store.get_points(scan_id)
    items, missing, size = [], [], 0
    for p in points:
        if not p["image_path"]:
            continue
        src = DATA_DIR / p["image_path"]
        if src.exists():
            items.append((p, src))
            size += src.stat().st_size
        else:
            missing.append(p["image_path"])
    if not items:
        # 预检就拦下来（别让人点了按钮才在后台线程里失败）：一条扫描要么有图可导，要么导出去
        # 只有一张空表和一句"什么都没有" —— 那不是导出，那是留个坑
        raise ValueError("这条扫描盘上一张图都没有，没什么可导的")
    return scan, points, items, missing, size


def _crop_records(scan_id: int, items: list) -> tuple:
    """这条扫描的裁剪记录：库里那份、文件里那份、以及照着查的那张图。

    两份必须一致 —— 取数时偏移只在读文件那一层减一次，记录对不上就等于"原点错了还不报错"。
    查**代表帧一张**就够：裁剪是一组一起动的（与裁剪预检同一个做法），逐张查只是多读几个 GB。
    """
    from .thorlabs_ccd import read_png_meta

    rec = store.get_crop(scan_id)
    db_rect = [rec["x0"], rec["y0"], rec["w"], rec["h"]] if rec else None
    if not items:
        return db_rect, None, None
    path = items[0][1]
    return db_rect, read_png_meta(path)["crop"], path.name


def _frame_head(path: Path) -> Optional[tuple]:
    """(宽, 高, 位深)；读不动就 None —— 图坏了照旧原样抄走，只是清单里如实写一句。"""
    from .thorlabs_ccd import png_ihdr

    try:
        return png_ihdr(path)
    except OSError:
        return None


def plan(scan_id: int, zipped: bool = False, stamp: Optional[str] = None) -> dict:
    """这次导出会得到什么 —— 面板上先说清楚，动手前就知道导到哪儿、有多少、缺什么。

    stamp 由调用方带回来（GET 先看一眼、POST 再动手），这样**面板上写的路径就是最终的路径**：
    每次现取的话，中间隔了几秒名字就变了，人照着面板去找会找不到。
    """
    scan, points, items, missing, size = _collect(scan_id)
    db_rect, file_rect, sample = _crop_records(scan_id, items)
    if db_rect != file_rect:
        raise ValueError(
            f"库里的裁剪记录和文件对不上（库 {db_rect or '没记录'}，"
            f"文件 {file_rect or '没记录'}，查的是 {sample}）—— 先查清再导："
            "原点偏移错了不会报错，只会让导出去的坐标整体错位")
    stamp = stamp or _stamp()
    factor = raw_readback_factor(scan.get("readback_to_um"))
    return {
        "scan_id": scan_id,
        "name": scan["name"],
        "status": scan["status"],
        "stamp": stamp,
        "zipped": bool(zipped),
        "dest": str(_free(_dest(scan, stamp, zipped))),
        "dir": str(EXPORT_DIR),
        "points": len(points),
        "images": len(items),
        "bytes": size,
        "missing": missing,
        "crop": db_rect,
        "sample": sample,
        "readback_to_um": scan.get("readback_to_um"),
        # 原值那一列有没有：没记下是哪台设备采的（老数据）、或系数本来就是 1（PI），
        # 都**不给**这一列 —— 判定只此一处（config.raw_readback_factor），界面照着写文案。
        "raw_available": factor is not None,
    }


def _rows(points: list, factor: Optional[float], missing: list) -> list:
    """每点一行 —— **没图的点也占一行**（空着并写明为什么）：点了几个点就该有几行。

    两种"没图"要分开写：压根没采到图（image_path 是空的）、和库里记着盘上却没有。
    后者在表里也留着原来的路径 —— 不然只看表的人不知道少的到底是哪一张。
    """
    gone = set(missing)
    rows = []
    for p in points:
        taken = p["taken_at"]
        why = ""
        if not p["image_path"]:
            why = "没采到图"
        elif p["image_path"] in gone:
            why = f"盘上找不到（库里记的是 {p['image_path']}）"
        rows.append({
            "idx": p["idx"],
            "target_um": p["target_um"],
            "actual_um": p["actual_um"],
            "readback_raw": (None if (factor is None or p["actual_um"] is None)
                             else p["actual_um"] / factor),
            "settle_ms": p["settled_ms"],
            "on_target": p["on_target"],
            "settle_source": p["settle_source"],
            "exposure_us": p["exposure_us"],
            "taken_at": taken,
            "taken_at_local": (datetime.fromtimestamp(taken).astimezone()
                               .isoformat(sep=" ", timespec="seconds") if taken else ""),
            "image": "",
            "image_bytes": "",
            "width": "", "height": "", "bits": "",
            "note": why,
        })
    return rows


def _csv_bytes(rows: list) -> bytes:
    """CSV 的字节：**带 BOM 的 UTF-8**、CRLF、表头就是 CSV_COLUMNS。

    BOM 是给 Excel 的：不带它，Windows 上的 Excel 会把中文读成乱码 —— 这张表多半就是
    双击打开的，为这一步多三个字节值。Python / MATLAB 那边会自己跳过 BOM。
    """
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\r\n")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


def _manifest(info: dict) -> dict:
    """清单：这一包是什么、从哪来、缺什么、口径是什么 —— 十年后翻出来也读得懂。

    每张 PNG 自己身上也有 tEXt（曝光、质心、裁剪矩形），这里再写一遍是因为**表和图要能对上**：
    只拿到 CSV 的人不用去解 PNG 就知道每张图的尺寸、以及这一组裁没裁过。
    """
    scan = info["scan"]
    head = _frame_head(info["items"][0][1]) if info["items"] else None
    notes = [
        "图是**原样复制**的：没裁过的就是相机原生全幅 16 位灰度；裁过的，盘上本来就是裁过的图"
        "（文件名不变、尺寸变小），裁剪矩形见 crop 一栏，每张 PNG 自己的 tEXt 里也有一份。",
        "actual_um 是**采图那一刻的读出位置**，不是命令值 target_um —— 两列都在表里，别混用。",
        "readback_raw = actual_um ÷ readback_to_um（设备报的原值）。这一列空着 = **没有这个数**"
        "（没记下是哪台设备采的，或这台设备报的本来就是 µm），不是 0。",
        "on_target 只是读数与命令值的比较，设备不拿它做任何判据（见 docs 里的设备认识账）。",
        "taken_at 是 Unix 秒（当时那台机器的钟），taken_at_local 是同一时刻的本地时间。",
    ]
    return {
        "kind": "pi-pzt：一次扫描的原始帧与每点元数据",
        "exported_at": datetime.fromtimestamp(info["now"]).astimezone().isoformat(
            sep=" ", timespec="seconds"),
        "exported_from": str(DATA_DIR),
        "scan": {
            "id": scan["id"], "name": scan["name"], "status": scan["status"],
            "message": scan["message"],
            "start_um": scan["start_um"], "stop_um": scan["stop_um"],
            "count": scan["count"], "settle_ms": scan["settle_ms"],
            "created_at": scan["created_at"], "finished_at": scan["finished_at"],
            "readback_to_um": scan["readback_to_um"],
        },
        "points": {
            "rows": len(info["rows"]),
            "images": len(info["items"]),
            "bytes": info["bytes"],
            "missing_images": info["missing"],
        },
        # 库与文件两边的裁剪记录（对不上就不让导，所以到这儿一定是相等的）
        "crop": {"db": info["crop_db"], "file": info["crop_file"],
                 "checked_on": info["sample"]},
        "frame": ({"width": head[0], "height": head[1], "bits": head[2]} if head else None),
        "files": {
            "points": CSV_NAME,
            "images": f"{len(info['items'])} 张 PNG，文件名与采集时一致",
            "packed": "zip（ZIP_STORED，只装不压）" if info["zipped"] else "目录（散图 + 两张表）",
        },
        "notes": notes,
    }


def _copy_frames(items: list, rows: list, sink: Callable, progress) -> None:
    """逐张交出去，顺手把每点的图名 / 尺寸 / 字节数填进那一行。

    读的是 **PNG 头 33 个字节**（不解码）：两千张也是眨眼的功夫，而"这张图多大、几位"
    是这张表能不能被别人正确解读的关键。读不动就照原样抄、只在 note 里说一句。
    """
    by_idx = {r["idx"]: r for r in rows}
    for n, (p, src) in enumerate(items, 1):
        row = by_idx[p["idx"]]
        head = _frame_head(src)
        row["image"] = src.name
        row["image_bytes"] = src.stat().st_size
        if head:
            row["width"], row["height"], row["bits"] = head
        else:
            row["note"] = "读不动 PNG 头（原样抄了一份，尺寸未知）"
        sink(src, src.name)
        if progress:
            progress(n, len(items))


def _run_dir(dest: Path, info: dict, progress) -> None:
    """散图 + points.csv + manifest.json。**manifest 最后写**：有它才算导完。"""
    dest.mkdir(parents=True)          # 名字是刚挑的空位；已经在就当场失败，不往里写
    try:
        def sink(src: Path, name: str) -> None:
            dst = dest / name
            shutil.copy2(src, dst)    # 连 mtime 一起抄：那是采图时刻的旁证
            if dst.stat().st_size != src.stat().st_size:
                raise OSError(f"{name} 抄过去大小不对"
                              f"（{dst.stat().st_size} ≠ {src.stat().st_size}）")

        _copy_frames(info["items"], info["rows"], sink, progress)
        (dest / CSV_NAME).write_bytes(_csv_bytes(info["rows"]))
        (dest / MANIFEST_NAME).write_text(
            json.dumps(_manifest(info), ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        # 删的是自己刚造的这一份（目标本来就是从空位里挑的）—— 源文件一个字节没动
        shutil.rmtree(dest, ignore_errors=True)
        raise


def _run_zip(dest: Path, info: dict, progress) -> None:
    """一个 zip（只装不压）+ 包内两张表。先写 .part，整包写完才改名。"""
    part = dest.with_name(dest.name + ".part")
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            def sink(src: Path, name: str) -> None:
                zf.write(src, arcname=name)

            _copy_frames(info["items"], info["rows"], sink, progress)
            zf.writestr(CSV_NAME, _csv_bytes(info["rows"]))
            zf.writestr(MANIFEST_NAME,
                        json.dumps(_manifest(info), ensure_ascii=False, indent=2))
        _verify_zip(part, info)
        os.replace(part, dest)
    except Exception:
        part.unlink(missing_ok=True)
        raise


def _verify_zip(part: Path, info: dict) -> None:
    """校验**落盘后的那个包**：条目数对不对、每一条记的大小是不是源文件的大小。

    只读中央目录（不解压、不重读数据）：ZIP_STORED 的数据是边读边写进去的，写的时候就在算 CRC，
    读源文件出错会当场抛 —— 这里要抓的是"包少了条目 / 大小不对"这一类。
    """
    want = {src.name: src.stat().st_size for _, src in info["items"]}
    want[CSV_NAME] = None
    want[MANIFEST_NAME] = None
    with zipfile.ZipFile(part) as zf:
        got = {i.filename: i.file_size for i in zf.infolist()}
    if len(got) != len(want):
        raise OSError(f"包里 {len(got)} 个条目，应该是 {len(want)} 个")
    for name, size in want.items():
        if name not in got:
            raise OSError(f"包里少了 {name}")
        if size is not None and got[name] != size:
            raise OSError(f"{name} 在包里的大小不对（{got[name]} ≠ {size}）")


def run(scan_id: int, zipped: bool = False, stamp: Optional[str] = None,
        progress=None) -> dict:
    """真干活：抄帧 + 写表 + 写清单。progress(done, total) 给界面看进度。

    stamp 由 plan 带回来（面板上写的就是这个路径）；不带就现取一个。
    返回的就是面板上要显示的东西：导到哪儿、多少张、多少字节、缺了什么。
    """
    scan, points, items, missing, size = _collect(scan_id)
    db_rect, file_rect, sample = _crop_records(scan_id, items)
    if db_rect != file_rect:
        raise ValueError(
            f"库里的裁剪记录和文件对不上（库 {db_rect or '没记录'}，"
            f"文件 {file_rect or '没记录'}，查的是 {sample}）—— 先查清再导")
    dest = _free(_dest(scan, stamp or _stamp(), zipped))
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    info = {
        "scan": scan, "items": items, "missing": missing, "bytes": size,
        "rows": _rows(points, raw_readback_factor(scan.get("readback_to_um")), missing),
        "crop_db": db_rect, "crop_file": file_rect, "sample": sample,
        "zipped": zipped, "now": time.time(),
    }
    (_run_zip if zipped else _run_dir)(dest, info, progress)
    log.info("导出扫描 #%s：%d 张 / %.1f MB → %s", scan_id, len(items), size / 1e6, dest)
    return {
        "scan_id": scan_id, "name": scan["name"], "zipped": bool(zipped),
        "path": str(dest), "points": len(points), "images": len(items),
        "bytes": size, "missing": missing,
        # 扫描还在跑的时候导的：后面还在采点，导出去的这份**不是完整的一次扫描**
        "status": scan["status"],
    }


def recent(limit: int = 10) -> list:
    """data/export/ 下已经有的东西 —— 回答"上次导哪儿了"。

    大小**不挨个 walk**（几千个文件会把面板拖慢）：目录读它自己的清单，zip 直接 stat。
    **没有清单的目录 = 没导完**（清单是最后一步写的），如实标出来，不混在成品里。
    """
    if not EXPORT_DIR.exists():
        return []
    out = []
    for p in sorted(EXPORT_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.name.endswith(".part") or p.name.startswith("."):
            continue                      # 半截包 / 临时文件不当成品列出来
        one = {"name": p.name, "path": str(p), "zipped": p.is_file(),
               "mtime": p.stat().st_mtime, "bytes": None, "images": None, "done": True}
        if p.is_dir():
            man = p / MANIFEST_NAME
            one["done"] = man.exists()      # 清单是最后一步写的：有它才算导完
            if one["done"]:
                try:
                    m = json.loads(man.read_text("utf-8"))
                    one["bytes"] = m["points"]["bytes"]
                    one["images"] = m["points"]["images"]
                except (OSError, ValueError, KeyError) as exc:
                    log.warning("读不动 %s：%s", man, exc)
        else:
            one["bytes"] = p.stat().st_size   # zip：整包写完才改名，在的就是成品
        out.append(one)
        if len(out) >= limit:
            break
    return out
