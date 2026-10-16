"""扫描数据包：一次扫描的**原始帧 + 每点元数据**，装成一个目录（或一个 zip）。

导出（export.py）与导入（importer.py）都从这里取名字和规矩 —— **包格式只有一个家**：
加一列、改一个键名、换一个文件名只改这里，两边同时跟着变。两处各写一份字面量的症状是
"导进来少一列，不报错"。

包里三样东西：

  * manifest.json —— 清单。它**在不在**本身就是"这包导完了没有"（导出时最后一步才写它）：
    没有清单的包不是包，导入直接拒收。
  * points.csv —— 每点一行，列名见 CSV_COLUMNS，一列什么意思见 NOTES。
  * 若干 PNG —— 文件名与采集时一致，整字节原样（不解码、不重编码）。

清单里写着格式版本（FORMAT）：将来加列 / 改结构时，导入方按它判断手上这包认不认得，
而不是"缺了字段就当没有" —— 那会把新格式读成旧格式，静默丢东西。
"""
from __future__ import annotations

import csv
import io
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# 包格式的版本：加一个**导入方必须知道**的结构（新列、新键）就 +1，并在这里决定旧版怎么办。
# 不问就往上加字段，等于把老包的读者留在原地 —— 1 是加这个字段之前就在用的那一版，
# 所以清单里**没有 format 的包按 1 读**（那时候导出去的包还躺在一堆硬盘上）。
FORMAT = 1
KIND = "pi-pzt：一次扫描的原始帧与每点元数据"
CSV_NAME = "points.csv"
MANIFEST_NAME = "manifest.json"

# 每点一行。列名用 ASCII：这张表是要拿出去给别的软件读的（Excel / Python / MATLAB），
# 中文列名一路要过编码的关。每一列什么意思写在 NOTES 与界面的导出面板上。
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

# 清单里那一串解释：给拿到包、手里没有本程序的人看的（十年后翻出来也读得懂）。
NOTES = (
    "图是**原样复制**的：没裁过的就是相机原生全幅 16 位灰度；裁过的，盘上本来就是裁过的图"
    "（文件名不变、尺寸变小），裁剪矩形见 crop 一栏，每张 PNG 自己的 tEXt 里也有一份。",
    "actual_um 是**采图那一刻的读出位置**，不是命令值 target_um —— 两列都在表里，别混用。",
    "readback_raw = actual_um ÷ readback_to_um（设备报的原值）。这一列空着 = **没有这个数**"
    "（没记下是哪台设备采的，或这台设备报的本来就是 µm），不是 0。",
    "on_target 只是读数与命令值的比较，设备不拿它做任何判据（见 docs 里的设备认识账）。",
    "taken_at 是 Unix 秒（当时那台机器的钟），taken_at_local 是同一时刻的本地时间。",
)


def stamp() -> str:
    """一批的标记：导出的目录名、导入的暂存目录名都用它 —— 一批一个名字，人看得懂就够。

    只到秒；同一秒里的第二批由调用方自己加序号（这是名字，不是身份）。
    """
    return time.strftime("%Y%m%d-%H%M%S")


def csv_bytes(rows: list) -> bytes:
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


def read_csv(data: bytes) -> list:
    """读回每点一行：表头必须**就是** CSV_COLUMNS。

    多一列 = 这包比本机认的格式新，少一列 = 缺东西。两种都不许"能读就读"：
    每一列都要落到库里（或界面上）的某个地方，含糊过去就是静默丢数据。
    """
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    header = reader.fieldnames or []
    if header != list(CSV_COLUMNS):
        raise ValueError(f"表头对不上：这包是 {header}，本机认的是 {list(CSV_COLUMNS)}")
    return list(reader)


def entry_name(name: str) -> str:
    """包内那一张图的名字 —— **只能是文件名本身**。

    CSV 是从外面来的东西：`../../x.png` 这种名字照着拼路径，就会写到包外面去
    （导入是唯一会照表里的名字写文件的地方）。空名字、带目录、绝对路径一律拒。
    """
    if not name or name in (".", "..") or name != Path(name).name or "\\" in name:
        raise ValueError(f"表里的图名不合法：{name!r}（只能是文件名，不许带路径）")
    return name


def manifest(scan: dict, *, rows: int, images: int, total_bytes: int, missing: list,
             crop: Optional[list], crop_file: Optional[list], checked_on: Optional[str],
             frame: Optional[tuple], zipped: bool, exported_from: str, now: float) -> dict:
    """清单：这一包是什么、从哪来、缺什么、口径是什么 —— 十年后翻出来也读得懂。

    每张 PNG 自己身上也有 tEXt（曝光、质心、裁剪矩形），这里再写一遍是因为**表和图要能对上**：
    只拿到 CSV 的人不用去解 PNG 就知道每张图的尺寸、以及裁没裁过。
    """
    return {
        "kind": KIND,
        "format": FORMAT,
        "exported_at": datetime.fromtimestamp(now).astimezone().isoformat(
            sep=" ", timespec="seconds"),
        "exported_from": exported_from,
        "scan": {
            "id": scan["id"], "name": scan["name"], "status": scan["status"],
            "message": scan["message"],
            "start_um": scan["start_um"], "stop_um": scan["stop_um"],
            "count": scan["count"], "settle_ms": scan["settle_ms"],
            "created_at": scan["created_at"], "finished_at": scan["finished_at"],
            "readback_to_um": scan["readback_to_um"],
        },
        "points": {"rows": rows, "images": images, "bytes": total_bytes,
                   "missing_images": list(missing)},
        # 库与文件两边的裁剪记录（导出时对不上就不让导，所以到这儿一定是相等的）
        "crop": {"db": crop, "file": crop_file, "checked_on": checked_on},
        "frame": ({"width": frame[0], "height": frame[1], "bits": frame[2]} if frame else None),
        "files": {
            "points": CSV_NAME,
            "images": f"{images} 张 PNG，文件名与采集时一致",
            "packed": "zip（ZIP_STORED，只装不压）" if zipped else "目录（散图 + 两张表）",
        },
        "notes": list(NOTES),
    }


def parse_manifest(data: dict) -> dict:
    """读一份清单：不是这包东西、或格式比本机新，都当场说清 —— 不猜、不"缺了就当没有"。"""
    if not isinstance(data, dict):
        raise ValueError("清单不是一份 JSON 对象")
    if data.get("kind") != KIND:
        raise ValueError(f"这不是本程序的扫描包（清单里写的是 {data.get('kind')!r}）")
    # 没有 format 的包 = 加这个字段之前导的，就是第 1 版（见 FORMAT 上面那段）
    fmt = data.get("format", FORMAT)
    if not isinstance(fmt, int) or fmt > FORMAT:
        raise ValueError(f"这包是格式 {fmt} 导的，本机认到 {FORMAT} —— 先更新程序再导它")
    out = {"format": fmt, "exported_at": data.get("exported_at", ""),
           "exported_from": data.get("exported_from", ""),
           "frame": data.get("frame") or None,
           "crop": data.get("crop") or {}}
    for key in ("scan", "points"):
        if not isinstance(data.get(key), dict):
            raise ValueError(f"清单里少了 {key} 一栏 —— 这包不完整")
        out[key] = data[key]
    return out
