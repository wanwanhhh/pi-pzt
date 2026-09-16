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
    image_path  TEXT,
    taken_at    REAL,
    PRIMARY KEY (scan_id, idx)
);
"""

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
    image_path: Optional[str],
) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO point"
            " (scan_id, idx, target_um, actual_um, settled_ms, on_target, image_path, taken_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (scan_id, idx, target_um, actual_um, settled_ms, int(on_target), image_path, time.time()),
        )


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
