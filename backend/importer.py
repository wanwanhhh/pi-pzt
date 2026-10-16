"""导入：把一份导出的包（一个目录，或一个 zip）恢复成本机库里的一条扫描。

**是"抄一份进来"，源包一个字节都不动** —— 与导出对称（那边是抄一份出去）。不碰设备、
也不排队进设备线程（跟数据页取数同一类），所以扫描跑着也能导；代价同样是跟采图抢磁盘。

落地顺序，每一档都在为"半截不算数"服务：

  1. 预检（plan）：这包认不认得、多少点多少图、放不放得下、号与图名撞不撞 —— **先看再做**。
     只读清单和那张表，不读图、不写任何东西。
  2. 逐张抄进 data/.import-<号>-<批>/（暂存）。这一档失败：删掉暂存目录就走，data/ 干干净净。
  3. 核对**落盘后的每一张**：字节数、尺寸、位深与表里那一行对齐；裁过的再对代表帧一张的 tEXt。
  4. 逐个改名就位到 data/images/。
  5. **最后才写库**（scan + points + crop，一个事务）。

  ③之前崩：盘上没有这一条的任何一张图。④之前崩：图在、库里没有 —— 界面上看不见它，
  重导会因为"盘上已经有同名的图"被拦住（说得清，人工删掉那几张再导）。
  **反过来没有这个退路**：先写库、后落图，中途崩了库里就记着一堆盘上没有的图，取数只会把
  那些点当"没图"断开，一句话都不说 —— 那才是最难发现的一种错。

扫描号**沿用包里那个**（图名 scan<号>_<序号>.png 就是照它起的）：本机已经有这个号、或者
盘上已经有同名的图，都在预检里拦下 —— **不覆盖任何东西**。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

from . import package, store
from .config import DATA_DIR, IMAGE_DIR

log = logging.getLogger(__name__)

# 暂存目录：data/.import-<扫描号>-<批次>/ —— 与裁剪的 .crop-<批>/ 并排躺在 data 下。
# 以点开头：列目录的地方（导出面板、图库）不会把它当成一条数据。
STAGE_PREFIX = ".import-"

# 清单的 scan 一栏里，写库时**一个都不能少**的键（其余的可以缺，缺了按空写）
_SCAN_KEYS = ("id", "name", "start_um", "stop_um", "count", "settle_ms", "status",
              "created_at", "finished_at", "readback_to_um")


class _Package:
    """一份包的两种读法（目录 / zip）—— 检查与抄写只写一遍。

    zip 的条目是边读边写的（ZIP_STORED 原样存着，读完顺手核 CRC，见 zipfile），
    目录就按路径读。除此之外两边一模一样：清单、表、每张图的字节。
    """

    def __init__(self, src: Path):
        self.src = src
        self.zipped = src.is_file()
        self._zf: Optional[zipfile.ZipFile] = None

    def __enter__(self) -> "_Package":
        if self.zipped:
            try:
                self._zf = zipfile.ZipFile(self.src)
            except zipfile.BadZipFile as exc:
                raise ValueError(f"{self.src.name} 不是个 zip：{exc}") from exc
        return self

    def __exit__(self, *exc) -> None:
        if self._zf is not None:
            self._zf.close()

    def entries(self) -> set:
        """包里有哪些文件（只算第一层）—— "这张图在不在包里"就靠它。"""
        if self._zf is not None:
            return {i.filename for i in self._zf.infolist() if not i.is_dir()}
        return {p.name for p in self.src.iterdir() if p.is_file()}

    def has(self, name: str) -> bool:
        if self._zf is not None:
            try:
                self._zf.getinfo(name)
            except KeyError:
                return False
            return True
        return (self.src / name).is_file()

    def read(self, name: str) -> bytes:
        if self._zf is not None:
            return self._zf.read(name)
        return (self.src / name).read_bytes()

    def copy(self, name: str, dst: Path) -> int:
        """抄一张出来，返回落盘的字节数 —— 调用方拿它对账。"""
        if self._zf is not None:
            with self._zf.open(name) as f, open(dst, "wb") as out:
                shutil.copyfileobj(f, out, 1 << 20)
        else:
            shutil.copy2(self.src / name, dst)      # 连 mtime 一起抄：那是采图时刻的旁证
        return dst.stat().st_size


def _open(src) -> _Package:
    """包在哪儿：一个目录，或者一个 .zip。别的一律当面说清。"""
    p = Path(str(src)).expanduser()
    if not p.exists():
        raise ValueError(f"找不到这个包：{p}")
    if p.is_file() and p.suffix.lower() != ".zip":
        raise ValueError(f"{p.name} 既不是目录也不是 zip —— 给包所在的那个目录，或者那个 .zip")
    return _Package(p)


def plan(src) -> dict:
    """这次导入会得到什么：哪一条、多少点、多少张、多大、盘上放不放得下 —— **动手前先看**。

    只读包里的两张表：点了这个接口，本机一个字节都不会变。
    """
    return _survey(src)[0]


def run(src, progress: Optional[Callable[[int, int], None]] = None) -> dict:
    """真干活：抄进暂存 → 核对 → 就位 → 写库。

    progress(done, total) 给界面看进度：total 是**两遍**图的长度 —— 前一遍抄、后一遍核对
    （就位那一步只是改名，快到不必报）。失败把暂存目录整个删掉（源包一个字节没动）。
    """
    shown, work = _survey(src)          # run 也可能被直接调用：预检再走一遍（只读两张表）
    to_copy = work["to_copy"]
    batch = DATA_DIR / f"{STAGE_PREFIX}{shown['scan_id']}-{package.stamp()}"
    try:
        batch.mkdir(parents=True)
    except FileExistsError as exc:      # 同一秒里的第二条，或者上次留下的
        raise ValueError(f"暂存目录 {batch.name} 已经在了 —— 先清掉它再来") from exc
    try:
        with _open(src) as pk:
            for n, row in enumerate(to_copy, 1):
                name = package.entry_name(row["image"])
                size = pk.copy(name, batch / name)
                want = _int(row["image_bytes"])
                if want is not None and size != want:
                    raise OSError(f"{name} 抄过来大小不对（{size} ≠ {want}）")
                if progress:
                    progress(n, max(2, len(to_copy) * 2))
            _verify_staged(batch, work, progress)
        for row in to_copy:                         # 全部核对过了才就位
            os.replace(batch / row["image"], IMAGE_DIR / row["image"])
        store.import_scan(work["scan"], _point_values(shown["scan_id"], work["rows"]),
                          _crop_values(shown))
    finally:
        shutil.rmtree(batch, ignore_errors=True)
    log.info("导入扫描 #%s：%d 点 / %d 张 ← %s", shown["scan_id"], shown["points"],
             shown["images"], shown["src"])
    return {"ok": True, **shown}


def sweep() -> int:
    """清掉上次进程被杀留下的暂存目录（源包不动，盘上只是多余的副本）。

    与裁剪那边不同：这里暂存的全是**刚抄进来、还没就位**的副本，库里也还没有这一条 ——
    直接删就是，没有要挪回去的东西。所以启动时无脑清，不用问它当时走到哪一步。
    """
    if not DATA_DIR.exists():
        return 0
    gone = 0
    for p in DATA_DIR.glob(f"{STAGE_PREFIX}*"):
        shutil.rmtree(p, ignore_errors=True)
        gone += 1
    if gone:
        log.info("清掉上次导入留下的暂存目录 %d 个（源包没动）", gone)
    return gone


def _survey(src) -> tuple:
    """把包看一遍：能导就给出「给人看的」与「干活用的」两份，不能导就抛 ValueError 说原因。

    这一步**只读清单与那张表**：不读图（zip 里的图不解开就读不到它的 tEXt）、
    不写任何东西 —— 逐张核对留给落盘之后（那时每一张都在本机，是文件不是条目）。
    """
    with _open(src) as pk:
        man = _read_manifest(pk)
        rows = package.read_csv(pk.read(package.CSV_NAME))
        to_copy, missing = _check_rows(rows, man, pk)
        crop = _crop_rect(man)
        scan = man["scan"]
        sid = int(scan["id"])
        if store.get_scan(sid) is not None:
            raise ValueError(
                f"本机已经有扫描 #{sid} 了 —— 导入不覆盖任何东西：先把本机那条删掉，"
                "或者把包拿到另一台机器上导")
        clash = [r["image"] for r in to_copy if (IMAGE_DIR / r["image"]).exists()]
        if clash:
            raise ValueError(
                f"盘上已经有 {len(clash)} 张同名的图（{clash[0]} …）—— 导入不覆盖任何东西："
                "先清掉那些文件（或弄清楚它们是哪一条扫描的）再来")
        need = sum(_int(r["image_bytes"]) or 0 for r in to_copy)
        free = shutil.disk_usage(DATA_DIR).free
        if need > free:
            raise ValueError(f"放不下：这一包 {need / 1e9:.2f} GB，盘上只剩 {free / 1e9:.2f} GB")
        shown = {
            "src": str(pk.src), "zipped": pk.zipped,
            "scan_id": sid, "name": scan.get("name", "") or "",
            "status": scan.get("status", ""), "message": scan.get("message", ""),
            "start_um": scan["start_um"], "stop_um": scan["stop_um"],
            "count": scan["count"], "settle_ms": scan["settle_ms"],
            "created_at": scan["created_at"], "finished_at": scan["finished_at"],
            "readback_to_um": scan["readback_to_um"],
            "points": len(rows), "images": len(to_copy), "bytes": need,
            "missing": missing,
            # 表里没写字节数的那些（导出时就读不动 PNG 头）：算不进"要多少空间"，也说一声
            "unknown_size": [r["image"] for r in to_copy if not r["image_bytes"]],
            "crop": crop, "frame": man.get("frame"),
            "exported_at": man["exported_at"], "exported_from": man["exported_from"],
            "dest_dir": str(IMAGE_DIR), "free_bytes": free,
        }
    return shown, {"man": man, "rows": rows, "to_copy": to_copy, "crop": crop, "scan": scan}


def _read_manifest(pk: _Package) -> dict:
    """清单。**它不在就是没导完**（导出时最后一步才写它）—— 半截的包不收。"""
    if not pk.has(package.MANIFEST_NAME):
        raise ValueError(f"这个包里没有 {package.MANIFEST_NAME} —— 没导完的包不收"
                         "（清单是导出最后一步写的，它不在就是当时没写完）")
    try:
        raw = json.loads(pk.read(package.MANIFEST_NAME).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"{package.MANIFEST_NAME} 读不动：{exc}") from exc
    man = package.parse_manifest(raw)
    lack = [k for k in _SCAN_KEYS if k not in man["scan"]]
    if lack:
        raise ValueError(f"清单的 scan 一栏里少了 {'、'.join(lack)} —— 这包不完整")
    return man


def _check_rows(rows: list, man: dict, pk: _Package) -> tuple:
    """表与包对不对得上：行数、序号、每一行的图在不在、图名干不干净。

    两种"没图"分开算：表里 image 空 = 这一点**本来就没采到图**（照原样恢复成没图）；
    表里记着名字、包里却没有 —— 只有清单**也**记着它缺（导出时就知道缺）才算如实，
    否则这包是拷了一半，不收。
    """
    declared = man["points"]
    if len(rows) != declared.get("rows"):
        raise ValueError(f"表里有 {len(rows)} 行，清单说 {declared.get('rows')} 行 —— 对不上")
    if [int(r["idx"]) for r in rows] != list(range(len(rows))):
        raise ValueError("表里的序号不是 0 起连着排的 —— 点号对不上库里的规矩，不敢导")
    known_gone = set(declared.get("missing_images") or [])
    entries = pk.entries()
    to_copy, missing = [], []
    for r in rows:
        name = (r["image"] or "").strip()
        if not name:
            continue                        # 这一点本来就没图
        package.entry_name(name)            # 图名带路径的（外面来的东西）当场拒
        if name in entries:
            to_copy.append(r)
        elif name in known_gone:
            missing.append(name)            # 导出时就知道盘上没有它，如实
        else:
            raise ValueError(f"包里没有 {name}，清单里也没说它缺 —— 这包拷了一半，不收")
    return to_copy, missing


def _crop_rect(man: dict) -> Optional[list]:
    """清单里那两份裁剪记录（库里一份、文件里一份）：**只认两边相等**。

    对不上就是"原点错了还不报错"那一类 —— 导出时不让导，导入时也不让进。
    """
    db = (man.get("crop") or {}).get("db")
    file = (man.get("crop") or {}).get("file")
    if db is None and file is None:
        return None
    if db != file:
        raise ValueError(f"清单里的裁剪记录自己对不上（库里 {db}，文件里 {file}）—— 不收")
    return [int(v) for v in db]


def _verify_staged(batch: Path, work: dict, progress=None) -> None:
    """核对**落盘后的每一张**：字节数、尺寸、位深与表里那一行对齐；裁过的再对代表帧一张。

    裁剪那边要逐张解 5 个点比像素，那是因为它**重写了每一张**；导入是整字节复制，
    字节数一样就说明没抄丢（zip 里那份还另过了 CRC），尺寸/位深从图上再读一遍，防的是
    "名字对了、内容不是那张图"。裁剪的两份记录到这里才是第一次能对着**文件**核 ——
    预检只读清单与表，zip 里的图不解开就读不到它自己的 tEXt。
    """
    from .thorlabs_ccd import png_ihdr, read_png_meta

    to_copy = work["to_copy"]
    frame = work["man"].get("frame") or {}
    total = max(2, len(to_copy) * 2)
    for n, row in enumerate(to_copy, 1):
        name = package.entry_name(row["image"])
        path = batch / name
        want = _int(row["image_bytes"])
        if want is not None and path.stat().st_size != want:
            raise OSError(f"{name} 落盘后大小不对（{path.stat().st_size} ≠ {want}）")
        head = png_ihdr(path)
        w, h, bits = _int(row["width"]), _int(row["height"]), _int(row["bits"])
        if None not in (w, h, bits) and head != (w, h, bits):
            raise OSError(f"{name} 的尺寸/位深与表里写的不一样：图上 {head}，"
                          f"表里 {w}×{h}、{bits} 位")
        if frame and (head[0], head[1]) != (frame["width"], frame["height"]):
            raise OSError(f"{name} 的尺寸与清单对不上：图上 {head[0]}×{head[1]}，"
                          f"清单 {frame['width']}×{frame['height']}（整条一起裁的，尺寸该一样）")
        if progress:
            progress(len(to_copy) + n, total)
    if work["crop"] and to_copy:
        name = to_copy[0]["image"]
        got = read_png_meta(batch / name).get("crop")
        if got != work["crop"]:
            raise OSError(f"图上写的裁剪矩形与清单对不上（图上 {got}，清单 {work['crop']}）——"
                          " 原点偏移错了不会报错，只会让整条曲线悄悄错位，所以这里必须停")


def _point_values(scan_id: int, rows: list) -> list:
    """库里的点：值全部照表抄，一个都不重算（没图的点 image_path 就是空）。

    `readback_raw` 那一列**不进库**：它是拿 actual_um 与系数现算出来的展示列 ——
    actual_um 与 readback_to_um 都在库里，谁要看谁算（见 config.raw_readback_factor）。
    """
    dir_name = IMAGE_DIR.relative_to(DATA_DIR)
    out = []
    for r in rows:
        name = (r["image"] or "").strip()
        out.append({
            "scan_id": scan_id,
            "idx": int(r["idx"]),
            "target_um": _num(r["target_um"]),
            "actual_um": _num(r["actual_um"]),
            "settled_ms": _num(r["settle_ms"]),
            "on_target": _int(r["on_target"]),
            "settle_source": (r["settle_source"] or "").strip() or None,
            "image_path": str(dir_name / name) if name else None,
            "exposure_us": _int(r["exposure_us"]),
            "taken_at": _num(r["taken_at"]),
        })
    return out


def _crop_values(shown: dict) -> Optional[dict]:
    """库里的裁剪记录：矩形照清单抄，**state 必须是 done**（取数只在 done 时按偏移读）。

    saved_bytes 写 None：那一格答的是"**这台机器上**裁掉了多少字节"，这条是搬进来的 ——
    没有这个数就是没有（界面上显示「—」），不拿 0 顶。
    created_at 用现在：这一刀在这台机器上是此刻落的账。
    """
    rect = shown["crop"]
    if not rect:
        return None
    x0, y0, w, h = rect
    return {"scan_id": shown["scan_id"], "x0": x0, "y0": y0,
            "w": w, "h": h, "frames": shown["images"], "saved_bytes": None,
            "state": "done", "created_at": time.time()}


def _num(text) -> Optional[float]:
    """表里的一格数字：空的 = **没有这个数**（不是 0）。"""
    text = str(text or "").strip()
    return float(text) if text else None


def _int(text) -> Optional[int]:
    text = str(text or "").strip()
    return int(float(text)) if text else None
