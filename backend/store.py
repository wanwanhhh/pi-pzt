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
    finished_at REAL
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
)

# 终止态：进程启动时把这两个状态之外的残留扫描判为 aborted
FINAL_STATUS = ("done", "aborted", "failed")


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
        # 上次进程被杀时留下的 running/paused 任务不可能再继续了
        conn.execute(
            "UPDATE scan SET status = 'aborted', message = '服务重启，任务中断',"
            " finished_at = ? WHERE status NOT IN (?, ?, ?)",
            (time.time(), *FINAL_STATUS),
        )


def create_scan(
    name: str, start_um: float, stop_um: float, count: int, settle_ms: int
) -> int:
    with _db() as conn:
        cur = conn.execute(
            # 直接写 running：这一行建好后立刻起扫描线程，pending 没有可观察的窗口
            "INSERT INTO scan (name, start_um, stop_um, count, settle_ms, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, 'running', ?)",
            (name, start_um, stop_um, count, settle_ms, time.time()),
        )
        return int(cur.lastrowid)


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
    with _db() as conn:
        rows = conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM point p WHERE p.scan_id = s.id) AS done"
            " FROM scan s ORDER BY s.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_scan(scan_id: int) -> Optional[dict[str, Any]]:
    with _db() as conn:
        row = conn.execute("SELECT * FROM scan WHERE id = ?", (scan_id,)).fetchone()
    return dict(row) if row else None


def get_points(scan_id: int, limit: int = 200_000) -> list[dict[str, Any]]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM point WHERE scan_id = ? ORDER BY idx LIMIT ?", (scan_id, limit)
        ).fetchall()
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
