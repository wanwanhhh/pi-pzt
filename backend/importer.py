"""导入：把一份导出的包（一个目录，或一个 zip）恢复成本机库里的一条扫描。

**是"抄一份进来"，源包一个字节都不动** —— 与导出对称（那边是抄一份出去）。不碰设备、
也不排队进设备线程（跟数据页取数同一类），所以扫描跑着也能导；代价同样是跟采图抢磁盘。

落地顺序，每一档都在为"半截不算数"服务：

  1. 预检（plan）：这包认不认得、多少点多少图、放不放得下、号与图名撞不撞 —— **先看再做**。
     只读清单和那张表，不读图、不写任何东西；写库要用的值（每点每格）也在这里解析完。
  2. **占号**：先写一行 status='importing'、0 个点的扫描（store.reserve_scan）。
     号一占住，扫描那边的 AUTOINCREMENT 就发不出它了 —— 不然抄图这几秒里正好撞上，
     导入会 os.replace 盖掉正在采的那张图。**占号先于落图**就是这个道理。
  3. 逐张抄进 data/.import-<号>-<批>/（暂存）。这一档失败：删暂存、撤掉占号的那一行。
  4. 核对**落盘后的每一张**：字节数、尺寸、位深与表里那一行对齐；裁过的再对代表帧一张的 tEXt。
  5. 逐个改名就位到 data/images/。
  6. **最后才写点**（points + crop，一个事务；scan 那一行由占号原地补成真扫描）。

  崩在 3、4（抄 / 核）：盘上还没有这一条的任何一张图。崩在 5、6（就位 / 写点）：
  图在、库里只有一行 importing（外加暂存目录里那张就位日志）—— 界面上看不见它，
  下次启动由 sweep() 连图带行清掉。
  **反过来没有这个退路**：先写点、后落图，中途崩了库里就记着一堆盘上没有的图，取数只会把
  那些点当"没图"断开，一句话都不说 —— 那才是最难发现的一种错。
  占号那一行一个点都没有，所以它**不会**变成"库里记着、盘上没有"：崩溃最坏也只是多一行
  importing + 几张图，sweep 认得出来。

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

# 暂存目录里那张**就位日志**：每挪一张之前先把它的名字写进去（并 flush）。
# 进程被杀在"图挪了一半、点还没写"那一档时，sweep 就靠它知道哪几张是这次搬进去的 ——
# 图名是包里的，不按 "scan<号>_*" 这种命名去猜（那是采图那边的规矩，不是包的规矩）。
PLACED_NAME = ".placed"

# 清单的 scan 一栏里**没有就写不出这一行**的键：号、行程、点数、稳定延时、建的时刻。
# 其余的（name / status / message / finished_at / readback_to_um）缺了按空写 —— 见 _scan_values。
_SCAN_KEYS = ("id", "start_um", "stop_um", "count", "settle_ms", "created_at")


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
    """真干活：**占号** → 抄进暂存 → 核对 → 就位 → 写点。

    progress(done, total) 给界面看进度：total 是**两遍**图的长度 —— 前一遍抄、后一遍核对
    （就位那一步只是改名，快到不必报）。失败把暂存目录整个删掉、把占着的那一行撤掉、
    把已经挪过去的图删掉（源包一个字节没动）。

    **号是第一步占的**（store.reserve_scan）：预检那一刻号还空着，抄图这几秒里扫描那边
    正好把它发出去，导入就会盖掉正在采的那张图。占住之后这个号谁也拿不走。

    写点那一步**只剩纯 INSERT**：每点每格的值在预检里就照表解析完了（见 _survey）——
    表里的 'abc' 那一格要在**动手之前**就拒，不能等到图都就位了才炸（那时盘上会留两张
    没人认领的图，同一包再也导不进来）。
    """
    shown, work = _survey(src)          # run 也可能被直接调用：预检再走一遍（只读两张表）
    to_copy = work["to_copy"]
    batch = DATA_DIR / f"{STAGE_PREFIX}{shown['scan_id']}-{package.stamp()}"
    moved: list = []                     # 已经从暂存改名就位的那几张（失败要挪回来删掉）
    reserved = False                     # 号是不是**这次**占的（没占成就不许去删别人的）
    try:
        try:
            batch.mkdir(parents=True)
        except FileExistsError as exc:  # 同一秒里的第二条，或者上次留下的
            raise ValueError(f"暂存目录 {batch.name} 已经在了 —— 先清掉它再来") from exc
        # **占号先于落图**（见 store.reserve_scan）：预检那一刻号是空的，抄图这几秒里
        # 扫描那边正好把这个号发出去的话，导入就会盖掉正在采的那张图。先占住，再抄。
        store.reserve_scan(work["scan"])
        reserved = True
        with _open(src) as pk:
            for n, row in enumerate(to_copy, 1):
                name = row["image"]              # 预检已经过了一遍 package.entry_name
                size = pk.copy(name, batch / name)
                want = _int(row["image_bytes"], "image_bytes 那一列")
                if want is not None and size != want:
                    raise OSError(f"{name} 抄过来大小不对（{size} ≠ {want}）")
                if progress:
                    progress(n, max(2, len(to_copy) * 2))
            _verify_staged(batch, work, progress)
        with open(batch / PLACED_NAME, "a", encoding="utf-8") as journal:
            for row in to_copy:                     # 全部核对过了才就位
                # 先记名字、再挪：崩在这两步之间时，sweep 至少知道这一张可能已经在盘上了
                # （下面那个 moved 管的是"进程还活着时的失败"，日志管的是"进程被杀"——
                #  两件事，缺一件就有一种失败会留下没人认领的图）
                journal.write(row["image"] + "\n")
                journal.flush()
                os.replace(batch / row["image"], IMAGE_DIR / row["image"])
                moved.append(row["image"])
        store.import_scan(work["scan"], work["points"], work["crop_row"])
    except Exception:
        # 任何失败都要回到原样：删掉**本次刚挪过去**的那几张（与导出失败时 rmtree 自己那个
        # 目录同一个道理），再撤掉占着的那一行。不删图的话盘上留下几张没人认领的图，
        # 同一包从此再也导不进来 —— 库里没有这一条，界面上看不见它们，只能人工去挑。
        for name in moved:
            (IMAGE_DIR / name).unlink(missing_ok=True)
        if reserved:
            store.drop_importing(shown["scan_id"])
        raise
    finally:
        shutil.rmtree(batch, ignore_errors=True)
    log.info("导入扫描 #%s：%d 点 / %d 张 ← %s", shown["scan_id"], shown["points"],
             shown["images"], shown["src"])
    return {"ok": True, **shown}


def sweep() -> int:
    """清掉上次进程被杀留下的导入现场：占着号的 scan 行、它已经落盘的图、暂存目录。

    与裁剪那边不同：这里暂存的全是**刚抄进来、还没就位**的副本，源包在包那边好好的 ——
    直接删就是，没有要挪回去的东西。所以启动时无脑清，不用问它当时走到哪一步。

    库里那一行（status='importing'，见 store.IMPORTING）与它已经就位的图也要一起收：
    那一行**不是一次扫描**（一个点都没采过），留着就会在历史表里冒充一条扫描；
    图留着就是几张没人认领的文件 —— 库里没有这一条，界面上看不见它们，
    而这一包再也导不进来（"盘上已经有同名的图"）。

    哪几张图是这次搬进去的：**暂存目录里那张就位日志**（PLACED_NAME）说了算。
    不按 "scan<号>_*" 这种命名去猜：那是采图那边的规矩，包里的图名可以是别的样子，
    照前缀猜会删错别人的图。日志不在（崩在写日志之前）就是一张都没就位，什么都不用删。

    必须在 store.init() **之后**跑（init 不再把 importing 改写成 aborted，见 store.init）：
    init 之前那一行还是上次留下的原样，这里才认得出来。
    """
    gone = 0
    for sid in store.importing_scans():
        for p in DATA_DIR.glob(f"{STAGE_PREFIX}{sid}-*"):
            journal = p / PLACED_NAME
            if journal.exists():
                for name in journal.read_text("utf-8").splitlines():
                    name = name.strip()
                    if name:
                        (IMAGE_DIR / name).unlink(missing_ok=True)
        store.drop_importing(sid)
        gone += 1
    if gone:
        log.info("清掉上次导入没跑完占的号 %d 个（连同它已经就位的图）", gone)
    if not DATA_DIR.exists():
        return gone
    for p in DATA_DIR.glob(f"{STAGE_PREFIX}*"):
        shutil.rmtree(p, ignore_errors=True)
        gone += 1
    if gone:
        log.info("清掉上次导入留下的现场 %d 处（源包没动）", gone)
    return gone


def _survey(src) -> tuple:
    """把包看一遍：能导就给出「给人看的」与「干活用的」两份，不能导就抛 ValueError 说原因。

    这一步**只读清单与那张表**：不读图（zip 里的图不解开就读不到它的 tEXt）、
    不写任何东西 —— 逐张核对留给落盘之后（那时每一张都在本机，是文件不是条目）。

    写库要用的三份值也**在这里就组装好**：表里每一格都在这一档解析完，写库那一步只剩纯
    INSERT。图都就位了才因为"某一格不是数字"炸掉，盘上会留几张没人认领的图 ——
    库里没有这一条，界面上看不见它们，同一包从此再也导不进来（见 run 的回收）。
    """
    with _open(src) as pk:
        man = _read_manifest(pk)
        rows = package.read_csv(pk.read(package.CSV_NAME))
        to_copy, missing = _check_rows(rows, man, pk)
        crop = _crop_rect(man)
        scan = man["scan"]
        # 写库要用的值**在这里就组装好**（表里每一格都解析一遍）：写库那一步只剩纯 INSERT，
        # 不会因为"某一格不是数字"在图都就位之后才失败 —— 那时盘上会留几张没人认领的图。
        scan_row = _scan_values(scan)
        sid = scan_row["id"]
        point_rows = _point_values(sid, rows, missing)
        crop_row = _crop_values(sid, crop, len(to_copy))
        if store.get_scan(sid) is not None:
            raise ValueError(
                f"本机已经有扫描 #{sid} 了 —— 导入不覆盖任何东西：先把本机那条删掉，"
                "或者把包拿到另一台机器上导")
        clash = [r["image"] for r in to_copy if (IMAGE_DIR / r["image"]).exists()]
        if clash:
            raise ValueError(
                f"盘上已经有 {len(clash)} 张同名的图（{clash[0]} …）—— 导入不覆盖任何东西："
                "先清掉那些文件（或弄清楚它们是哪一条扫描的）再来")
        need = sum(_int(r["image_bytes"], "image_bytes 那一列") or 0 for r in to_copy)
        free = shutil.disk_usage(DATA_DIR).free
        if need > free:
            raise ValueError(f"放不下：这一包 {need / 1e9:.2f} GB，盘上只剩 {free / 1e9:.2f} GB")
        shown = {
            "src": str(pk.src), "zipped": pk.zipped,
            "scan_id": sid, "name": scan_row["name"],
            "status": scan_row["status"], "message": scan_row["message"],
            "start_um": scan_row["start_um"], "stop_um": scan_row["stop_um"],
            "count": scan_row["count"], "settle_ms": scan_row["settle_ms"],
            "created_at": scan_row["created_at"], "finished_at": scan_row["finished_at"],
            "readback_to_um": scan_row["readback_to_um"],
            "points": len(rows), "images": len(to_copy), "bytes": need,
            "missing": missing,
            # 表里没写字节数的那些（导出时就读不动 PNG 头）：算不进"要多少空间"，也说一声
            "unknown_size": [r["image"] for r in to_copy if not r["image_bytes"]],
            "crop": crop, "frame": man.get("frame"),
            "exported_at": man["exported_at"], "exported_from": man["exported_from"],
            "dest_dir": str(IMAGE_DIR), "free_bytes": free,
        }
    return shown, {"man": man, "to_copy": to_copy, "crop": crop,
                   "scan": scan_row, "points": point_rows, "crop_row": crop_row}


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
    """表与包对不对得上：行数、每一行的图在不在、图名干不干净、有没有两行指同一张。

    （序号 0 起连着排那一条在 _point_values：那里才解析 idx，这里只比行数。）

    两种"没图"分开算：表里 image 空 = 这一点**本来就没采到图**（照原样恢复成没图）；
    表里记着名字、包里却没有 —— 只有清单**也**记着它缺（导出时就知道缺）才算如实，
    否则这包是拷了一半，不收。

    返回 (要抄的行, {文件名: 清单里记的那条路径})。后者是"库里记着、盘上没有"的那些：
    包里只有文件名，**原来那条路径在清单里**（missing_images），导入时照它写回 image_path ——
    不然往返一次，库里记的路径就永久变成"没采到图"了。
    """
    declared = man["points"]
    if len(rows) != declared.get("rows"):
        raise ValueError(f"表里有 {len(rows)} 行，清单说 {declared.get('rows')} 行 —— 对不上")
    # 清单记的是**库里那条路径**（images/scan0001_00001.png），表里记的是文件名。
    # 同一条路径记了两遍（导出侧理论上不会有）就整包不收 —— 不收的理由是"这包自己说不清
    # 哪一行缺"，不是"按第一个认"。
    known_gone: dict = {}
    for p in declared.get("missing_images") or []:
        name = Path(str(p)).name
        if known_gone.setdefault(name, p) != p:
            raise ValueError(f"清单里把 {name} 记成了两条不同的路径"
                             f"（{known_gone[name]}、{p}）—— 这包对不上，不收")
    entries = pk.entries()
    to_copy, gone = [], {}
    for r in rows:
        name = (r["image"] or "").strip()
        if not name:
            continue                        # 这一点本来就没图
        r["image"] = package.entry_name(name)   # 图名带路径的（外面来的东西）当场拒
        if name in entries and name in known_gone:
            # 清单说这张盘上没有、包里却又装着一张：这包自己前后矛盾，不收
            raise ValueError(f"清单说 {name} 缺、包里却有这张 —— 这包对不上，不收")
        if name in entries:
            to_copy.append(r)
        elif name in known_gone:
            # 导出时就知道盘上没有它：照清单里那条**原路径**写回（往返一次路径不许变）
            gone[name] = _db_path(known_gone[name], name)
        else:
            raise ValueError(f"包里没有 {name}，清单里也没说它缺 —— 这包拷了一半，不收")
    twice = sorted({r["image"] for r in to_copy
                    if sum(1 for o in to_copy if o["image"] == r["image"]) > 1})
    if twice:
        # 两行指着同一张图：就位时第二张会 FileNotFoundError，那时图已经挪过去一半了
        raise ValueError(f"表里有两行指着同一张图（{twice[0]}）—— 这包对不上，不收")
    return to_copy, gone


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
    if not isinstance(db, list) or len(db) != 4:
        raise ValueError(f"清单里的裁剪矩形不是四个数：{db!r} —— 这包对不上，不收")
    rect = [_int(v, "清单里的裁剪矩形") for v in db]
    if None in rect:
        raise ValueError(f"清单里的裁剪矩形有空的一格：{db!r} —— 这包对不上，不收")
    return rect


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
        name = row["image"]                      # 预检已经过了一遍 package.entry_name
        path = batch / name
        want = _int(row["image_bytes"], "image_bytes 那一列")
        if want is not None and path.stat().st_size != want:
            raise OSError(f"{name} 落盘后大小不对（{path.stat().st_size} ≠ {want}）")
        head = png_ihdr(path)
        w, h, bits = (_int(row["width"], "width 那一列"), _int(row["height"], "height 那一列"),
                      _int(row["bits"], "bits 那一列"))
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


def _scan_values(scan: dict) -> dict:
    """库里那一行扫描：清单的 scan 一栏照抄，缺的键按空写（**不猜**，也不编一个 0）。

    缺了写不出来的那几格（号、行程、点数、稳定延时、建的时刻）在 _read_manifest 就拒了，
    走到这儿一定有。缺了按空写的是这几格：name / status / message / finished_at /
    readback_to_um —— 它们答的是"叫什么、怎么结束的、哪台设备采的"，没有就是没有。
    """
    def text(key: str) -> str:
        value = scan.get(key)
        return "" if value is None else str(value)

    def num(key: str) -> float:
        value = _num(scan.get(key), f"清单 scan 一栏的 {key}")
        if value is None:
            raise ValueError(f"清单 scan 一栏的 {key} 是空的 —— 这包不完整")
        return value

    def whole(key: str) -> int:
        return int(num(key))

    return {
        "id": whole("id"),
        "name": text("name"),
        "start_um": num("start_um"),
        "stop_um": num("stop_um"),
        "count": whole("count"),
        "settle_ms": whole("settle_ms"),
        "status": text("status"),
        "message": text("message"),
        "created_at": num("created_at"),
        "finished_at": _num(scan.get("finished_at"), "清单 scan 一栏的 finished_at"),
        "readback_to_um": _num(scan.get("readback_to_um"), "清单 scan 一栏的 readback_to_um"),
    }


def _point_values(scan_id: int, rows: list, gone: dict) -> list:
    """库里的点：值全部照表抄，一个都不重算（没图的点 image_path 就是空）。

    `readback_raw` 那一列**不进库**：它是拿 actual_um 与系数现算出来的展示列 ——
    actual_um 与 readback_to_um 都在库里，谁要看谁算（见 config.raw_readback_factor）。

    三档"图"：表里有名字、包里也有 → 就位后的路径；表里有名字、清单记着盘上没有（gone）
    → 照清单里那条**原路径**写回（往返一次路径不许变）；表里空着 → 没采到图。

    表里每一格都在这里解析（写库那一步只剩纯 INSERT），序号 0 起连着排也在这一档核。
    """
    def num(row: dict, key: str) -> Optional[float]:
        return _num(row[key], f"表里的 {key} 那一列")

    def whole(row: dict, key: str) -> Optional[int]:
        return _int(row[key], f"表里的 {key} 那一列")

    def index(row: dict) -> int:
        value = whole(row, "idx")
        if value is None:
            raise ValueError("表里的 idx 那一列有空的一格 —— 这包对不上，不收")
        return value

    out = []
    for r in rows:
        name = (r["image"] or "").strip()
        out.append({
            "scan_id": scan_id,
            "idx": index(r),
            "target_um": num(r, "target_um"),
            "actual_um": num(r, "actual_um"),
            "settled_ms": num(r, "settle_ms"),
            "on_target": whole(r, "on_target"),
            "settle_source": (r["settle_source"] or "").strip() or None,
            "image_path": (gone[name] if name in gone else
                           _image_path(name) if name else None),
            "exposure_us": whole(r, "exposure_us"),
            "taken_at": num(r, "taken_at"),
        })
    if [p["idx"] for p in out] != list(range(len(rows))):
        raise ValueError("表里的序号不是 0 起连着排的 —— 点号对不上库里的规矩，不敢导")
    return out


def _image_path(name: str) -> str:
    """图在库里的路径：`images/<文件名>`，**一律用 / **。

    与采图那边写进库的式样一致。用 str(Path(...)) 在 Windows 上会写成反斜杠 ——
    库里就出现两种路径式样，往返对比（导入后与导出前一模一样）当场就红，
    取数/导出这边只是碰巧还能认。
    """
    return f"{IMAGE_DIR.relative_to(DATA_DIR).as_posix()}/{name}"


def _db_path(path, name: str) -> str:
    """清单里记的那条路径**照抄进库**（它就是这条扫描在这台机器上原来那条）。

    只认"它指的确实是包里这个名字"：路径的末段必须就是 `name`，也不许是绝对路径 /
    带 `..` —— 清单是外面来的，照它写进库的路径将来会被 `DATA_DIR / path` 拼成读盘的位置。
    """
    text = str(path).replace("\\", "/")      # 库里一律用 /（Windows 上采的包可能带反斜杠）
    if Path(text).is_absolute() or ".." in Path(text).parts or Path(text).name != name:
        raise ValueError(f"清单里 missing_images 记的路径不合法：{path!r}"
                         f"（应该是库里那种 images/{name}）—— 这包对不上，不收")
    return text


def _crop_values(scan_id: int, rect: Optional[list], frames: int) -> Optional[dict]:
    """库里的裁剪记录：矩形照清单抄，**state 必须是 done**（取数只在 done 时按偏移读）。

    saved_bytes 写 None：那一格答的是"**这台机器上**裁掉了多少字节"，这条是搬进来的 ——
    没有这个数就是没有（界面上显示「—」），不拿 0 顶。
    created_at 用现在：这一刀在这台机器上是此刻落的账。
    """
    if not rect:
        return None
    x0, y0, w, h = rect
    return {"scan_id": scan_id, "x0": x0, "y0": y0,
            "w": w, "h": h, "frames": frames, "saved_bytes": None,
            "state": "done", "created_at": time.time()}


def _num(value, where: str = "表里的一格") -> Optional[float]:
    """一格数字：空的 = **没有这个数**（不是 0）。

    空只认 None 与空串：**0 是个数**（起点、读数都可能是 0），不能被当成"没写"。
    where 是这一格的名字（列名 / 清单里的键）—— 这包是外面来的，"abc" 那一格要当场说清
    是哪一格，人才知道去改哪儿。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"{where} 不是个数：{text!r} —— 这包对不上，不收") from None


def _int(value, where: str = "表里的一格") -> Optional[int]:
    num = _num(value, where)
    return None if num is None else int(num)
