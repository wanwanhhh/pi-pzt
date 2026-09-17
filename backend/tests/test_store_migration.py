"""store 迁移回归：老库没有 settle_source 列时，init() 必须补上。

CREATE TABLE IF NOT EXISTS 对已存在的表什么都不做 —— 少了补列这一步，
老 data/scans.db 上插点会因为缺列直接失败。这个陷阱是真的。

直接跑：python backend/tests/test_store_migration.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend import store
from backend.stage_api import SETTLE_SOFTWARE, SETTLE_UNKNOWN

OLD_SCHEMA = """
CREATE TABLE scan (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    start_um REAL NOT NULL,
    stop_um REAL NOT NULL,
    count INTEGER NOT NULL,
    settle_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    message TEXT,
    created_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE point (
    scan_id INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    target_um REAL NOT NULL,
    actual_um REAL,
    settled_ms REAL,
    on_target INTEGER,
    image_path TEXT,
    taken_at REAL,
    PRIMARY KEY (scan_id, idx)
);
"""


def _query(db: Path, sql: str) -> list:
    # 注意：sqlite3.connect 的 with 只提交事务，**不关连接**；
    # Windows 上残留连接会让临时目录删不掉（WinError 32）。用 closing。
    with closing(sqlite3.connect(db)) as con:
        return con.execute(sql).fetchall()


class _TempStore:
    """把 store 的三个路径指到临时目录，跑完还原。"""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmp.name)
        self.db = root / "scans.db"
        self._saved = (store.DATA_DIR, store.IMAGE_DIR, store.DB_PATH)
        store.DATA_DIR, store.IMAGE_DIR, store.DB_PATH = root, root / "images", self.db
        return self

    def __exit__(self, *exc):
        store.DATA_DIR, store.IMAGE_DIR, store.DB_PATH = self._saved
        self._tmp.cleanup()
        return False


def test_old_db_gets_the_new_column():
    with _TempStore() as t:
        with closing(sqlite3.connect(t.db)) as con:
            con.executescript(OLD_SCHEMA)
            con.commit()

        store.init()

        cols = {r[1] for r in _query(t.db, "PRAGMA table_info(point)")}
        assert "settle_source" in cols, f"迁移没补列，现有列 {sorted(cols)}"


def test_add_point_round_trips_settle_source():
    with _TempStore() as t:
        store.init()
        scan_id = store.create_scan("迁移测试", 0.0, 10.0, 2, 100)
        store.add_point(scan_id, 0, 1.0, 1.0, 5.0, True, SETTLE_SOFTWARE, None)
        got = _query(t.db, "SELECT settle_source FROM point")
        assert got and got[0][0] == SETTLE_SOFTWARE, got


# 模拟「上一版迁移已经补过列、但还没有回填逻辑」的库：
# 列在、行值是 NULL。光测「原本没有这一列」的库拦不住这种情形。
COLUMN_ADDED_SCHEMA = OLD_SCHEMA.replace(
    "    on_target INTEGER,", "    on_target INTEGER,\n    settle_source TEXT,"
)


def test_null_rows_are_backfilled_even_when_column_already_exists():
    """回填不能只在 ALTER 分支里跑 —— 否则上一次启动补的列会永远留着 NULL"""
    with _TempStore() as t:
        with closing(sqlite3.connect(t.db)) as con:
            con.executescript(COLUMN_ADDED_SCHEMA)
            con.execute(
                "INSERT INTO point (scan_id, idx, target_um, actual_um, settled_ms,"
                " on_target, settle_source, image_path, taken_at)"
                " VALUES (1, 0, 1.0, 1.0, 5.0, 1, NULL, NULL, 0.0)"
            )
            con.commit()
        store.init()
        got = _query(t.db, "SELECT settle_source FROM point")
        assert got and got[0][0] == SETTLE_UNKNOWN, f"残留 NULL：{got}"


def test_migrated_schema_covers_fresh_schema():
    """OLD_SCHEMA 是手写近似，只能验 ALTER 通路；
    这条保证迁移后的列集合至少覆盖新建库的列集合，别的漂移也拦得住。"""
    with _TempStore() as old:
        with closing(sqlite3.connect(old.db)) as con:
            con.executescript(OLD_SCHEMA)
            con.commit()
        store.init()
        old_cols = {r[1] for r in _query(old.db, "PRAGMA table_info(point)")}
    with _TempStore() as fresh:
        store.init()
        fresh_cols = {r[1] for r in _query(fresh.db, "PRAGMA table_info(point)")}
    missing = fresh_cols - old_cols
    assert not missing, f"迁移后的老库缺列 {sorted(missing)}"


def test_old_rows_get_a_defined_settle_source():
    """补列之后旧行不能留 NULL —— get_points 是 SELECT * 原样吐出去的"""
    with _TempStore() as t:
        with closing(sqlite3.connect(t.db)) as con:
            con.executescript(OLD_SCHEMA)
            con.execute(
                "INSERT INTO point (scan_id, idx, target_um, actual_um, settled_ms,"
                " on_target, image_path, taken_at) VALUES (1, 0, 1.0, 1.0, 5.0, 1, NULL, 0.0)"
            )
            con.commit()
        store.init()
        got = _query(t.db, "SELECT settle_source FROM point")
        assert got and got[0][0] == SETTLE_UNKNOWN, got


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"FAIL  {name}\n      {type(exc).__name__}: {exc}")
        else:
            print(f"ok    {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
