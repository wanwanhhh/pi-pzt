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
        assert "exposure_us" in cols, f"曝光列没补上，现有列 {sorted(cols)}"
        scols = {r[1] for r in _query(t.db, "PRAGMA table_info(scan)")}
        assert "readback_to_um" in scols, f"位置读回口径列没补上，现有列 {sorted(scols)}"
        # 老扫描**不猜**口径：NULL = 未记录（猜一个系数会让整条曲线的横轴静默错位）
        got = _query(t.db, "SELECT readback_to_um FROM scan")
        assert got == [], got


def test_add_point_round_trips_settle_source():
    with _TempStore() as t:
        store.init()
        scan_id = store.create_scan("迁移测试", 0.0, 10.0, 2, 100, 1.0)
        store.add_point(scan_id, 0, 1.0, 1.0, 5.0, True, SETTLE_SOFTWARE, None)
        got = _query(t.db, "SELECT settle_source FROM point")
        assert got and got[0][0] == SETTLE_SOFTWARE, got


def test_add_point_round_trips_exposure():
    """曝光随点入库；**没有**时留 NULL（不许倒填当前值）。"""
    with _TempStore() as t:
        store.init()
        scan_id = store.create_scan("曝光", 0.0, 10.0, 2, 100, 1.0)
        store.add_point(scan_id, 0, 1.0, 1.0, 5.0, True, SETTLE_SOFTWARE, "images/a.png", 12_000)
        store.add_point(scan_id, 1, 2.0, 2.0, 5.0, True, SETTLE_SOFTWARE, None)
        got = _query(t.db, "SELECT exposure_us FROM point ORDER BY idx")
        assert [r[0] for r in got] == [12_000, None], got


def test_scan_readback_scale_round_trips():
    """建扫描时抄下设备的口径；老扫描可以事后标一次（只改这一个字段）。"""
    with _TempStore() as t:
        store.init()
        scan_id = store.create_scan("口径", 0.0, 10.0, 2, 100, 0.75)
        assert store.get_scan(scan_id)["readback_to_um"] == 0.75
        store.set_scan_readback(scan_id, 1.0)
        assert store.get_scan(scan_id)["readback_to_um"] == 1.0
        store.set_scan_readback(scan_id, None)          # 清回未记录
        assert store.get_scan(scan_id)["readback_to_um"] is None, _query(
            t.db, "SELECT readback_to_um FROM scan"
        )


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


# 老 crop 表：按**同名那一组**记（group_name），"省了多少字节"是 NOT NULL。
# 裁剪改成一条一条来（用户 2026-10 定）、导入进来的记录又没有那个数，两条都得改。
OLD_CROP_SCHEMA = """
CREATE TABLE crop (
    scan_id     INTEGER PRIMARY KEY REFERENCES scan(id) ON DELETE CASCADE,
    group_name  TEXT    NOT NULL,
    x0          INTEGER NOT NULL,
    y0          INTEGER NOT NULL,
    w           INTEGER NOT NULL,
    h           INTEGER NOT NULL,
    frames      INTEGER NOT NULL,
    saved_bytes INTEGER NOT NULL,
    state       TEXT    NOT NULL,
    created_at  REAL    NOT NULL
);
"""


def test_crop_table_is_rebuilt_one_row_per_scan():
    """老 crop 表 → 去 group_name、saved_bytes 允许空，**老行的数一个不能丢**。

    SQLite 既删不掉列也改不了 NOT NULL，只能重建。矩形丢了就是取数时偏移错位，
    而且不会报错 —— 所以这条盯的是"原样搬过去"。
    """
    with _TempStore() as t:
        with closing(sqlite3.connect(t.db)) as con:
            con.executescript(OLD_SCHEMA + OLD_CROP_SCHEMA)
            # crop.scan_id 指着一条真扫描（重建时外键是开着的：孤儿记录是坏库，不该悄悄放过）
            con.execute("INSERT INTO scan VALUES (7, '同名那一组', 0, 1, 2, 100, 'done', '', 1.0, 2.0)")
            con.execute("INSERT INTO crop VALUES (7, '同名那一组', 200, 300, 500, 700,"
                        " 11, 6525350, 'done', 1.0)")
            con.commit()
        store.init()
        store.init()                                  # 幂等：第二次启动不许再动它

        assert store.get_crop(7) == {"scan_id": 7, "x0": 200, "y0": 300, "w": 500, "h": 700,
                                    "frames": 11, "saved_bytes": 6525350, "state": "done",
                                    "created_at": 1.0}, store.get_crop(7)
        # 重建之后那个数可以**没有**（导入进来的记录本机没量过）—— 老表的 NOT NULL 拦得住这一步
        sid = store.create_scan("导入来的", 0.0, 1.0, 1, 100, None)
        store.put_crops([{"scan_id": sid, "x0": 1, "y0": 2, "w": 30, "h": 40, "frames": 0,
                          "saved_bytes": None, "state": "done", "created_at": 2.0}])
        assert store.get_crop(sid)["saved_bytes"] is None, store.get_crop(sid)


# 一版更早的 crop 表：有 group_name，**没有** saved_bytes（那个数是后来才记的）。
OLD_CROP_NO_SAVED = """
CREATE TABLE crop (
    scan_id     INTEGER PRIMARY KEY REFERENCES scan(id) ON DELETE CASCADE,
    group_name  TEXT    NOT NULL,
    x0          INTEGER NOT NULL,
    y0          INTEGER NOT NULL,
    w           INTEGER NOT NULL,
    h           INTEGER NOT NULL,
    frames      INTEGER NOT NULL,
    state       TEXT    NOT NULL,
    created_at  REAL    NOT NULL
);
"""


def test_a_crop_table_that_cannot_be_rebuilt_is_refused_without_damage():
    """老 crop 表缺一列（saved_bytes）：明确拒绝，库**原样不动**，再跑一次还是同一句话。

    实测过的样子：executescript 会先把已执行的语句提交，于是报错之后库里同时留着 crop 与
    crop_new；再跑一次撞「table crop_new already exists」，这个库从此打不开。
    """
    with _TempStore() as t:
        with closing(sqlite3.connect(t.db)) as con:
            con.executescript(OLD_SCHEMA + OLD_CROP_NO_SAVED)
            con.execute("INSERT INTO scan VALUES (7, '老扫描', 0, 1, 2, 100, 'done', '', 1.0, 2.0)")
            con.execute("INSERT INTO crop VALUES (7, '同名那一组', 200, 300, 500, 700,"
                        " 11, 'done', 1.0)")
            con.commit()

        said = []
        for _ in range(2):                      # 再跑一次：要给出**同一句**可解释的错
            try:
                store.init()
            except ValueError as exc:
                said.append(str(exc))
            else:
                raise AssertionError("老表缺列，重建本该明确拒绝")
        assert "saved_bytes" in said[0] and said[0] == said[1], said

        # 库原样：crop 还是老样子、老行一个不少、没有半截的 crop_new
        tables = {r[0] for r in _query(t.db, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "crop_new" not in tables, f"失败之后不许留半截的表：{sorted(tables)}"
        cols = {r[1] for r in _query(t.db, "PRAGMA table_info(crop)")}
        assert "group_name" in cols and "saved_bytes" not in cols, sorted(cols)
        assert _query(t.db, "SELECT scan_id, x0, y0, w, h FROM crop") == [(7, 200, 300, 500, 700)]

        # 把缺的那一列补上（人照着那句话办）：这一次要成功，老行原样搬过去
        with closing(sqlite3.connect(t.db)) as con:
            con.execute("ALTER TABLE crop ADD COLUMN saved_bytes INTEGER NOT NULL DEFAULT 0")
            con.commit()
        store.init()
        got = store.get_crop(7)
        assert got is not None and got["saved_bytes"] == 0, got
        assert got["x0"] == 200 and got["h"] == 700, got


def test_orphan_crop_rows_are_refused_by_name_not_by_a_half_built_table():
    """老 crop 表里有孤儿行（指着的扫描已经不在了）：说清是哪几条，库不动。

    外键开着，那几行搬不过去。悄悄丢掉就是丢数据；留半截的表就是把这个库弄坏。
    """
    with _TempStore() as t:
        with closing(sqlite3.connect(t.db)) as con:
            con.executescript(OLD_SCHEMA + OLD_CROP_SCHEMA)
            con.execute("INSERT INTO scan VALUES (7, '还在的', 0, 1, 2, 100, 'done', '', 1.0, 2.0)")
            con.execute("INSERT INTO crop VALUES (7, '还在的', 200, 300, 500, 700,"
                        " 11, 6525350, 'done', 1.0)")
            con.commit()
            con.execute("PRAGMA foreign_keys = OFF")     # 造一条当年能写进去的孤儿行
            con.execute("INSERT INTO crop VALUES (99, '没了的', 1, 2, 3, 4, 5, 6, 'done', 1.0)")
            con.commit()

        for _ in range(2):
            try:
                store.init()
            except ValueError as exc:
                assert "99" in str(exc), exc
            else:
                raise AssertionError("有孤儿行，重建本该明确拒绝")

        tables = {r[0] for r in _query(t.db, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "crop_new" not in tables, f"失败之后不许留半截的表：{sorted(tables)}"
        assert len(_query(t.db, "SELECT * FROM crop")) == 2, "老行一个都不许丢"

        # 按那句话把孤儿行删掉：这一次要成功，剩下的那行原样搬过去
        with closing(sqlite3.connect(t.db)) as con:
            con.execute("DELETE FROM crop WHERE scan_id = 99")
            con.commit()
        store.init()
        assert store.get_crop(7)["saved_bytes"] == 6525350, store.get_crop(7)


def test_a_leftover_crop_new_from_an_old_failure_is_dropped_and_rebuilt():
    """上一次半途而废留下的 crop_new：DROP IF EXISTS 掉重来（里面那半份不算数）。"""
    with _TempStore() as t:
        with closing(sqlite3.connect(t.db)) as con:
            con.executescript(OLD_SCHEMA + OLD_CROP_SCHEMA)
            con.execute("INSERT INTO scan VALUES (7, '同名那一组', 0, 1, 2, 100, 'done', '', 1.0, 2.0)")
            con.execute("INSERT INTO crop VALUES (7, '同名那一组', 200, 300, 500, 700,"
                        " 11, 6525350, 'done', 1.0)")
            # 半截产物：形状对、里面只搬了一半
            con.execute("CREATE TABLE crop_new (scan_id INTEGER PRIMARY KEY, x0 INTEGER, y0 INTEGER,"
                        " w INTEGER, h INTEGER, frames INTEGER, saved_bytes INTEGER, state TEXT,"
                        " created_at REAL)")
            con.commit()
        store.init()
        assert store.get_crop(7)["w"] == 500 and store.get_crop(7)["saved_bytes"] == 6525350


def test_a_rebuild_that_fails_midway_rolls_back_to_the_old_table():
    """重建本身半路失败（这里让搬列那一步撞一个不存在的列）：库里还是老 crop 表，不留 crop_new。

    走的是显式事务那一档（逐条 execute、失败 ROLLBACK）—— executescript 会先把已执行的语句
    提交掉，于是失败之后库里同时留着 crop 与 crop_new，下一次启动就撞「表已存在」。
    """
    with _TempStore() as t:
        with closing(sqlite3.connect(t.db)) as con:
            con.executescript(OLD_SCHEMA + OLD_CROP_SCHEMA)
            con.execute("INSERT INTO scan VALUES (7, '同名那一组', 0, 1, 2, 100, 'done', '', 1.0, 2.0)")
            con.execute("INSERT INTO crop VALUES (7, '同名那一组', 200, 300, 500, 700,"
                        " 11, 6525350, 'done', 1.0)")
            con.commit()

        class _Broken:
            """搬列那一步失败：前三条（DROP/CREATE/INSERT）都执行过，第四条炸掉。

            要的就是"已经执行了几条"这个现场 —— 那正是 executescript 会留下的半截。
            """

            def __init__(self, conn):
                self._conn = conn
                self.in_transaction = conn.in_transaction

            def execute(self, sql, *args):
                if sql.lstrip().startswith("DROP TABLE crop"):
                    raise sqlite3.OperationalError("模拟：删老表这一步失败")
                return self._conn.execute(sql, *args)

        with closing(sqlite3.connect(t.db)) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys = ON")
            try:
                store._rebuild_crop(_Broken(con))
            except ValueError as exc:
                assert "库里还是原样" in str(exc), exc
            else:
                raise AssertionError("半路失败却没抛")

        tables = {r[0] for r in _query(t.db, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "crop_new" not in tables, f"失败之后不许留半截的表：{sorted(tables)}"
        assert _query(t.db, "SELECT scan_id, group_name, x0 FROM crop") == [(7, "同名那一组", 200)]
        # 回到原样之后，正常那一遍仍然走得通
        store.init()
        assert store.get_crop(7)["saved_bytes"] == 6525350


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
