"""SQLite 持久化：扫描任务与每点元数据。

写入频率很低（每点一次，秒级），所以连接即用即关，不做连接池。
WAL 模式让"执行器写入"与"界面读取"互不阻塞。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from .config import DATA_DIR, DB_PATH, IMAGE_DIR
from .stage_api import SETTLE_UNKNOWN

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL DEFAULT '',
    start_um    REAL    NOT NULL,
    stop_um     REAL    NOT NULL,
    count       INTEGER NOT NULL,
    settle_ms   INTEGER NOT NULL,
    status      TEXT    NOT NULL,
    message     TEXT    NOT NULL DEFAULT '',
    created_at  REAL    NOT NULL,
    finished_at REAL,
    -- 这条扫描的位置读回口径：**µm = 读回值 × 这个系数**（设备属性，建扫描时从设备抄下来）。
    -- NULL = 未记录（加这一列之前的老数据；界面上可以手工标一次，见 server.set_scan_readback）。
    -- 数据页「口径」按钮靠它把已存的 µm 反推成设备原值：**库里的数一个字节都不动**。
    readback_to_um REAL
);

CREATE TABLE IF NOT EXISTS point (
    scan_id     INTEGER NOT NULL REFERENCES scan(id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    target_um   REAL    NOT NULL,
    actual_um   REAL,
    settled_ms  REAL,
    on_target   INTEGER,
    settle_source TEXT,
    image_path  TEXT,
    -- 这一点**采图那一刻**用的曝光（µs，相机读回值）。NULL = 未记录：
    -- 占位相机没有曝光概念、没采到图的点、以及加这一列之前的老数据。
    exposure_us INTEGER,
    taken_at    REAL,
    PRIMARY KEY (scan_id, idx)
);

-- 手动保存的原始帧（「保存原生帧」存下的那些）。
-- **与扫描各点的图分开**：扫描图归 point.image_path，和它自己的扫描绑在一起；
-- 这里是独立的一张张帧，可以各自命名。
-- filename 就是主键：改名时**磁盘文件名和这条记录必须一起改**（见 server.rename_grab），
-- 只改一边就是"库里有、点开 404"或者"文件还在、列表里没了"。
-- 保存时的撞名由调用方避开（thorlabs_ccd.save_raw 发现同名就加 _2），
-- 下面的 ON CONFLICT 只是"同一张重复登记"的兜底，不是覆盖别人的入口。
CREATE TABLE IF NOT EXISTS grab (
    filename    TEXT PRIMARY KEY,
    created_at  REAL NOT NULL
);

-- 数据页「裁剪」的记录：**一条扫描一行** —— 裁一次只动这一条，不牵连别的扫描
-- （曾经按同名整组一起裁，用户 2026-10 改成一条一条来：组那个概念没有了）。
-- 口径是**原始坐标**：x0/y0 是这一刀在原始帧里的左上角，(w,h) 是裁下来的尺寸。
-- 取数时只在读文件那一层做一次减法（x_file = x - x0），别处一律原始坐标。
-- 和 PNG 自己 tEXt 里的 CropRect 各记一份，对不上就报错 —— 原点偏移错了不会报错，
-- 只会让曲线整体错位，两份记录是唯一能自动发现它的地方。
-- state：cropping 是"正在动文件"（进程被杀会留下这个状态，启动时按下面 recover 的规矩收尾），
-- done 才是裁完了。**只有 done 才允许取数按偏移读**（cropping 期间那些文件可能是缺的）。
CREATE TABLE IF NOT EXISTS crop (
    scan_id     INTEGER PRIMARY KEY REFERENCES scan(id) ON DELETE CASCADE,
    x0          INTEGER NOT NULL,
    y0          INTEGER NOT NULL,
    w           INTEGER NOT NULL,
    h           INTEGER NOT NULL,
    frames      INTEGER NOT NULL,
    -- 这一刀省下多少字节（实测：原文件大小 − 裁后大小；同一条再裁就**累加**）。
    -- NULL = **没有这个数**：导入进来的记录是别的机器上裁的，这台机器没量过 —— 不拿 0 顶。
    saved_bytes INTEGER,
    state       TEXT    NOT NULL,
    created_at  REAL    NOT NULL
);
"""

# 已有库的补列。CREATE TABLE IF NOT EXISTS 对已存在的表**什么都不做**，
# 不加这一步，老 data/scans.db 上插点会因为缺列直接失败。
# 表名/列名是模块常量，不是外部输入。
MIGRATIONS = (
    # (表, 列, 类型, 老行补什么值)
    ("point", "settle_source", "TEXT", SETTLE_UNKNOWN),
    # 曝光对老行**没有**能补的值（当时没记），如实留 NULL —— 界面显示「—」，
    # 不许拿"现在的曝光"去倒填：那是编数，不是记录。
    ("point", "exposure_us", "INTEGER", None),
    # 老扫描没记是哪台设备采的，**不许猜一个系数填上**（猜错 = 整条曲线的横轴静默错位）。
    # 留 NULL 就是"未记录"，数据页只能标一次之后才能切原值。
    ("scan", "readback_to_um", "REAL", None),
)

# 终止态：进程启动时把这两个状态之外的残留扫描判为 aborted
FINAL_STATUS = ("done", "aborted", "failed")

# 导入占号时写的那一行的状态：号已经占了，图还没就位、点还没写。
# 它**不是一次扫描**（一个点都没采过），所以不进 FINAL_STATUS，也不许被启动时的
# aborted 回写改写成"一条中断的扫描"混进历史表 —— 那是给导入没完成、下次启动由
# importer.sweep() 连图带行清掉的那一档（见 init 与 importer.sweep）。
IMPORTING = "importing"


# 重建时要从老 crop 表搬过去的列。列名只写这一处（下面的 INSERT ... SELECT 用同一串）——
# 老表缺了其中一列就没法搬（那不是本程序这一版之前的形状），当场说清，不动库。
_CROP_COLUMNS = ("scan_id", "x0", "y0", "w", "h", "frames", "saved_bytes", "state", "created_at")


def _rebuild_crop(conn: sqlite3.Connection) -> None:
    """把老的 crop 表改成"一条扫描一行"：去掉 group_name、saved_bytes 允许空。

    裁剪不再按同名整组走（用户 2026-10 定），group_name 答的那个问题已经不存在了；
    导入进来的记录也没有"省了多少字节"这个实测值，留空比填 0 诚实。
    SQLite 既删不掉列、也改不了 NOT NULL，只能重建 —— 老行的数一个不落地搬过去，
    幂等（列没了就是已经重建过）。

    **整段是一个事务**（逐条 execute，不用 executescript）：那个函数会先把已经执行的语句
    提交掉，于是建了一半的表会留在库里，下一次启动撞上"table crop_new already exists"，
    这个库从此打不开。失败要么回到原样、要么留一句能照着办的话 —— 不许留半截。
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(crop)")}
    if not have or "group_name" not in have:
        return
    lack = [c for c in _CROP_COLUMNS if c not in have]
    if lack:
        raise ValueError(
            f"crop 表还是老的（有 group_name），却缺 {'、'.join(lack)} 这几列 —— 重建没法把"
            "老记录搬过去。库没动：先弄清楚这个库是哪来的，或者把它备份掉再开程序")
    # 孤儿行（指着的扫描已经不在了）搬不过去（外键开着）：不悄悄丢，当场说清是哪几条
    orphans = [r["scan_id"] for r in conn.execute(
        "SELECT scan_id FROM crop WHERE scan_id NOT IN (SELECT id FROM scan)")]
    if orphans:
        raise ValueError(
            f"老 crop 表里有 {len(orphans)} 行指着的扫描已经不在了"
            f"（scan_id {orphans[:5]}）—— 重建搬不过去。库没动：先把这些行删掉再来"
            f"（DELETE FROM crop WHERE scan_id IN ({', '.join(str(int(i)) for i in orphans[:5])})）")
    own = not conn.in_transaction
    if own:
        # 自己开一个：_db() 的隐式事务只认 INSERT/UPDATE/DELETE，DDL 会跑在自动提交里
        conn.execute("BEGIN IMMEDIATE")
    try:
        # 上一次半途而废留下的那张：DROP 掉重来（它里面的数没搬完，crop 才是准的）
        conn.execute("DROP TABLE IF EXISTS crop_new")
        conn.execute("""
            CREATE TABLE crop_new (
                scan_id     INTEGER PRIMARY KEY REFERENCES scan(id) ON DELETE CASCADE,
                x0          INTEGER NOT NULL,
                y0          INTEGER NOT NULL,
                w           INTEGER NOT NULL,
                h           INTEGER NOT NULL,
                frames      INTEGER NOT NULL,
                saved_bytes INTEGER,
                state       TEXT    NOT NULL,
                created_at  REAL    NOT NULL
            )
        """)
        cols = ", ".join(_CROP_COLUMNS)
        conn.execute(f"INSERT INTO crop_new ({cols}) SELECT {cols} FROM crop")
        conn.execute("DROP TABLE crop")
        conn.execute("ALTER TABLE crop_new RENAME TO crop")
    except sqlite3.Error as exc:
        if own:
            conn.execute("ROLLBACK")
        raise ValueError(f"重建 crop 表失败（库里还是原样，修好再来）：{exc}") from exc
    if own:
        conn.execute("COMMIT")
    log.info("crop 表已重建：去掉 group_name、saved_bytes 允许空（老记录原样搬过去）")


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    with _db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        for table, column, kind, backfill in MIGRATIONS:
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            # 回填放在 if **外面**、无条件跑：列可能是上一次启动补的，那时还没有回填逻辑，
            # 老行会一直是 NULL。WHERE ... IS NULL 让它幂等，每次启动跑一遍不花钱。
            conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE {column} IS NULL", (backfill,)
            )
        _rebuild_crop(conn)
        # 上次进程被杀时留下的 running/paused 任务不可能再继续了。
        # importing **不算**（见 IMPORTING）：那一行是导入占的号，一个点都没采过，
        # 改成 aborted 就会在历史表里冒充一条"中断的扫描" —— 它归 importer.sweep() 删。
        conn.execute(
            "UPDATE scan SET status = 'aborted', message = '服务重启，任务中断',"
            " finished_at = ? WHERE status NOT IN (?, ?, ?, ?)",
            (time.time(), *FINAL_STATUS, IMPORTING),
        )


def create_scan(
    name: str,
    start_um: float,
    stop_um: float,
    count: int,
    settle_ms: int,
    readback_to_um: float,
) -> int:
    with _db() as conn:
        cur = conn.execute(
            # 直接写 running：这一行建好后立刻起扫描线程，pending 没有可观察的窗口
            "INSERT INTO scan (name, start_um, stop_um, count, settle_ms, status, created_at,"
            " readback_to_um) VALUES (?, ?, ?, ?, ?, 'running', ?, ?)",
            (name, start_um, stop_um, count, settle_ms, time.time(), readback_to_um),
        )
        return int(cur.lastrowid)


def set_scan_readback(scan_id: int, readback_to_um: Optional[float]) -> None:
    """标一次「这条扫描是哪台设备采的」—— 只写口径，**不碰任何测量数据**。

    老扫描（这一列之前没有）没记设备，反推原值必须知道系数；系数只能由人指认。
    """
    with _db() as conn:
        conn.execute(
            "UPDATE scan SET readback_to_um = ? WHERE id = ?", (readback_to_um, scan_id)
        )


def set_status(scan_id: int, status: str, message: str = "") -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE scan SET status = ?, message = ? WHERE id = ?",
            (status, message, scan_id),
        )


def finish_scan(scan_id: int, status: str, message: str = "") -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE scan SET status = ?, message = ?, finished_at = ? WHERE id = ?",
            (status, message, time.time(), scan_id),
        )


def add_point(
    scan_id: int,
    idx: int,
    target_um: float,
    actual_um: float,
    settled_ms: float,
    on_target: bool,
    settle_source: str,
    image_path: Optional[str],
    exposure_us: Optional[int] = None,
) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO point"
            " (scan_id, idx, target_um, actual_um, settled_ms, on_target, settle_source,"
            "  image_path, exposure_us, taken_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (scan_id, idx, target_um, actual_um, settled_ms, int(on_target), settle_source,
             image_path, exposure_us, time.time()),
        )


def reserve_scan(scan: dict[str, Any]) -> int:
    """**先把号占住**：写一行 status='importing'、0 个点的扫描，返回它。

    导入要抄几秒到几分钟的图，号是那一刻现取的（AUTOINCREMENT）—— 预检时号还空着，
    抄图期间 store.create_scan 正好把这个号发出去，导入随后 os.replace 就盖掉了
    正在采的那张图，最后撞主键。所以**占号先于落图**：先占，再抄。

    占不到（这个号已经在了）就当场拒 —— 导入不覆盖任何东西。
    这一行由 import_scan 原地补成真扫描，或者由 drop_importing 删掉。
    """
    with _db() as conn:
        try:
            conn.execute(
                "INSERT INTO scan (id, name, start_um, stop_um, count, settle_ms, status,"
                " message, created_at, finished_at, readback_to_um)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)",
                (scan["id"], scan["name"], scan["start_um"], scan["stop_um"], scan["count"],
                 scan["settle_ms"], IMPORTING, scan["created_at"], scan["finished_at"],
                 scan["readback_to_um"]),
            )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" not in str(exc):
                raise                     # 不是撞号：那是别的问题，别拿"号被占了"盖住它
            raise ValueError(f"本机已经有扫描 #{scan['id']} 了 —— 导入不覆盖任何东西") from exc
    return int(scan["id"])


def drop_importing(scan_id: int) -> None:
    """撤掉导入占的那一行（点由外键级联删；这条扫描一个点都没采过）。"""
    with _db() as conn:
        conn.execute("DELETE FROM scan WHERE id = ? AND status = ?", (scan_id, IMPORTING))


def importing_scans() -> list[int]:
    """上次进程被杀在导入半路时留下的那些号（启动时由 importer.sweep 清掉）。"""
    with _db() as conn:
        rows = conn.execute("SELECT id FROM scan WHERE status = ?", (IMPORTING,)).fetchall()
    return [int(r["id"]) for r in rows]


def import_scan(scan: dict[str, Any], points: list[dict[str, Any]],
                crop: Optional[dict[str, Any]]) -> int:
    """把一份导入包恢复成库里的一条扫描：占着的那一行 + 全部点（+ 裁剪记录），**一个事务**。

    与"边采边写"那条路（create_scan → add_point → finish_scan）分工不同：这里写的是
    **一次已经结束的扫描**，值全部来自包里的记录，一个都不重算：

    * `id` **沿用包里那个号** —— 图名是 scan<号>_<序号>.png，换号会让文件名与扫描号对不上。
      这个号由 reserve_scan 先占住（那一行此刻是 importing），这里把它**原地**补成真扫描：
      不新插一行，因为"同一个号两行"根本插不进去。
    * `taken_at` 是**采图那一刻**（包里记的），不是"现在"：搬过来的是当时那一次扫描。
    * 裁剪记录里 `saved_bytes` 写 NULL：那一格答的是"**这台机器上**裁掉了多少字节"，
      这条扫描在这台机器上没裁过 —— 没有这个数就是没有，不拿 0 顶。
    """
    with _db() as conn:
        cur = conn.execute(
            "UPDATE scan SET name = :name, start_um = :start_um, stop_um = :stop_um,"
            " count = :count, settle_ms = :settle_ms, status = :status, message = :message,"
            " created_at = :created_at, finished_at = :finished_at,"
            " readback_to_um = :readback_to_um WHERE id = :id AND status = :importing",
            {**scan, "importing": IMPORTING},
        )
        if cur.rowcount != 1:
            raise ValueError(f"扫描 #{scan['id']} 没有占着（应该先 reserve_scan）")
        conn.executemany(
            "INSERT INTO point (scan_id, idx, target_um, actual_um, settled_ms, on_target,"
            " settle_source, image_path, exposure_us, taken_at)"
            " VALUES (:scan_id, :idx, :target_um, :actual_um, :settled_ms, :on_target,"
            " :settle_source, :image_path, :exposure_us, :taken_at)",
            points,
        )
        if crop is not None:
            conn.execute(
                "INSERT INTO crop (scan_id, x0, y0, w, h, frames, saved_bytes, state, created_at)"
                " VALUES (:scan_id, :x0, :y0, :w, :h, :frames, :saved_bytes, :state,"
                " :created_at)",
                crop,
            )
    return int(scan["id"])


def register_grab(filename: str) -> None:
    """记下这一帧；已经有记录就不动它的名字（重复保存同名文件时才走 upsert）。"""
    with _db() as conn:
        conn.execute(
            "INSERT INTO grab (filename, created_at) VALUES (?, ?)"
            " ON CONFLICT(filename) DO UPDATE SET created_at = excluded.created_at",
            (filename, time.time()),
        )


def list_grabs() -> list[dict[str, Any]]:
    with _db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM grab ORDER BY created_at DESC")]


def rename_grab(old: str, new: str) -> None:
    """把一帧的**文件名**改成 new（磁盘上真改），同时更新库里的记录。

    只改库不动文件，库里就会指向一个不存在的文件 —— 所以这两步必须一起做。
    真正的改名动作（同名冲突、非法字符、路径穿越的检查）在 server 层，
    因为那里才知道 IMAGE_DIR 在哪、以及要给用户报什么错。
    """
    with _db() as conn:
        conn.execute("UPDATE grab SET filename = ? WHERE filename = ?", (new, old))


def delete_grab(filename: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM grab WHERE filename = ?", (filename,))


def delete_grabs(filenames: list[str]) -> None:
    with _db() as conn:
        conn.executemany("DELETE FROM grab WHERE filename = ?", [(f,) for f in filenames])


def list_scans(limit: int = 50) -> list[dict[str, Any]]:
    """历史扫描表：最近的那几条（新在前）。

    **导入占着号的那些不列**（status='importing'，见 IMPORTING）：那一行不是一次扫描
    （一个点都没采过），列出来就会在历史表里冒充一条 —— 而它几秒后要么变成真扫描、
    要么被删掉。
    """
    with _db() as conn:
        rows = conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM point p WHERE p.scan_id = s.id) AS done"
            " FROM scan s WHERE s.status != ? ORDER BY s.id DESC LIMIT ?",
            (IMPORTING, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_scan(scan_id: int) -> Optional[dict[str, Any]]:
    with _db() as conn:
        row = conn.execute("SELECT * FROM scan WHERE id = ?", (scan_id,)).fetchone()
    return dict(row) if row else None


def get_points(scan_id: int, limit: Optional[int] = 200_000) -> list[dict[str, Any]]:
    """这条扫描的点，按序号升序。

    limit 是**防呆**（界面上的表用不着更多），不是"一条扫描就这么长"：要全部点的
    （导出：一行一个点，少一行就是少一个点）传 None —— 拿默认值去导会静默截断，
    表比扫描的点数短、清单里两个数还自相矛盾。
    """
    sql = "SELECT * FROM point WHERE scan_id = ? ORDER BY idx"
    args: tuple = (scan_id,)
    if limit is not None:
        sql += " LIMIT ?"
        args += (limit,)
    with _db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def delete_scan(scan_id: int) -> None:
    """删扫描连同它的图像。point 行由外键级联删除，这里只先取出图像路径。"""
    with _db() as conn:
        rows = conn.execute(
            "SELECT image_path FROM point WHERE scan_id = ? AND image_path IS NOT NULL",
            (scan_id,),
        ).fetchall()
        conn.execute("DELETE FROM scan WHERE id = ?", (scan_id,))
    for row in rows:
        # 尽力而为：文件被占用/只读不该让已经删掉的 DB 行回滚成 500
        try:
            (DATA_DIR / row["image_path"]).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("删除图像 %s 失败：%s", row["image_path"], exc)


# ------------------------------------------------------------------ 数据页：裁剪
# 记录本身只管"这一刀在哪、什么状态"；动文件那套在 thorlabs_ccd.crop_frames 里。
# **先落库（cropping）再动文件**：进程被杀时靠这条记录判断该往哪边收尾（见 recover_crops）。

def get_crop(scan_id: int) -> Optional[dict[str, Any]]:
    with _db() as conn:
        row = conn.execute("SELECT * FROM crop WHERE scan_id = ?", (scan_id,)).fetchone()
    return dict(row) if row else None


def all_crops() -> list[dict[str, Any]]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM crop").fetchall()
    return [dict(r) for r in rows]


def put_crops(records: list[dict[str, Any]]) -> None:
    """裁剪记录的写入：**一个事务**（半截记录比没有记录更难查）。

    同一条**再裁**是更新同一条记录（scan_id 就是主键）：矩形换成新的，
    saved_bytes **累加**（那个数问的是"这条一共省了多少"，不是"最后一刀省了多少"）。
    原来**没有**这个数的（导入进来的记录）就从 0 起算 —— COALESCE 那一步。
    created_at 不动 —— 它记的是这一条第一次挨裁的时刻。
    """
    with _db() as conn:
        conn.executemany(
            "INSERT INTO crop (scan_id, x0, y0, w, h, frames, saved_bytes, state, created_at)"
            " VALUES (:scan_id, :x0, :y0, :w, :h, :frames, :saved_bytes, :state, :created_at)"
            " ON CONFLICT(scan_id) DO UPDATE SET"
            " x0 = excluded.x0, y0 = excluded.y0, w = excluded.w, h = excluded.h,"
            " frames = excluded.frames,"
            " saved_bytes = COALESCE(crop.saved_bytes, 0) + excluded.saved_bytes,"
            " state = excluded.state",
            records,
        )


def finish_crop(scan_id: int, frames: int, saved_bytes: int) -> None:
    """裁完了：补上这条实际裁了多少张、省了多少字节，并把状态改成 done。"""
    with _db() as conn:
        conn.execute(
            "UPDATE crop SET frames = ?, saved_bytes = COALESCE(saved_bytes, 0) + ?,"
            " state = 'done' WHERE scan_id = ?",
            (frames, saved_bytes, scan_id),
        )


def set_crop_rects(records: list[dict[str, Any]]) -> None:
    """把矩形改回**某一批开工之前**的样子（进程被杀在裁剪半路时收尾用）。

    只动矩形：frames/saved_bytes 是"干过多少活"的流水，不是坐标，改回去反而对不上。
    """
    with _db() as conn:
        conn.executemany(
            "UPDATE crop SET x0 = :x0, y0 = :y0, w = :w, h = :h WHERE scan_id = :scan_id",
            records,
        )


def set_crop_state(scan_ids: list[int], state: str) -> None:
    with _db() as conn:
        conn.executemany("UPDATE crop SET state = ? WHERE scan_id = ?",
                         [(state, sid) for sid in scan_ids])


def delete_crops(scan_ids: list[int]) -> None:
    with _db() as conn:
        conn.executemany("DELETE FROM crop WHERE scan_id = ?", [(sid,) for sid in scan_ids])
