"""토큰 원장 계정별 저장 — 원장 재구성 마이그레이션(PK 변경)과 계정 규칙 모듈(server/accounts.py).

운영 DB(data/madison.db)는 열지 않는다: 모든 DB는 임시 디렉터리의 파일(동시 기동·롤백 증명에 파일이 필요)이거나
:memory:다. db.conn() 경로도 CFG.db_path를 임시 경로로 바꿔서만 부른다.
"""
import base64
import json
import os
import random
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from server import accounts, db, state, tokens
from server.config import REPO_ROOT

NEW_PK = ("device_id", "agent", "account", "project", "model", "frontend", "source")
SUM_COLS = ("input", "output", "cache_read", "cache_write", "thinking", "turns")


def old_ledger_ddl(name: str, key: str) -> str:
    """account 열 이전의 원장 DDL(옛 PK) — 마이그레이션 입력을 만드는 데만 쓴다."""
    return f"""CREATE TABLE {name} (
  {key} TEXT NOT NULL, device_id INTEGER NOT NULL, agent TEXT NOT NULL,
  project TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', frontend TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'events',
  input INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0, cache_read INTEGER NOT NULL DEFAULT 0,
  cache_write INTEGER NOT NULL DEFAULT 0, thinking INTEGER NOT NULL DEFAULT 0, turns INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY ({key}, device_id, agent, project, model, frontend, source))"""


def pk_columns(c, table):
    info = [r for r in c.execute(f"PRAGMA table_info({table})") if r[5]]
    return tuple(r[1] for r in sorted(info, key=lambda r: r[5]))


def columns(c, table):
    return tuple(r[1] for r in c.execute(f"PRAGMA table_info({table})"))


def totals(c, table):
    sums = ",".join(f"COALESCE(SUM({col}),0)" for col in SUM_COLS)
    return tuple(c.execute(f"SELECT COUNT(*), {sums} FROM {table}").fetchone())


def tables(c):
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def seed_legacy(c, *, hourly=True, n=400, seed=7):
    """옛 형태(account 없음)의 일·시간 원장을 만들고 다양한 키·큰 값으로 채운다. 반환: 기대 합계."""
    c.executescript(db._BASE_SCHEMA)          # 새 형태의 token_daily가 생긴다 — 옛 형태로 바꿔 치운다
    c.execute("DROP TABLE token_daily")
    c.execute(old_ledger_ddl("token_daily", "day"))
    c.execute("CREATE INDEX idx_token_daily_day ON token_daily(day)")
    if hourly:
        c.execute("DROP TABLE IF EXISTS token_hourly")
        c.execute(old_ledger_ddl("token_hourly", "hour"))
    c.execute("ALTER TABLE sessions DROP COLUMN account")   # 계정 열이 없던 시절의 sessions
    for did, name in ((1, "acme-mini"), (2, "acme-hub")):
        c.execute("INSERT OR IGNORE INTO devices (id,name,token_hash,created_at) VALUES (?,?,'x','t')", (did, name))
    rnd = random.Random(seed)
    for table, key, fmt in (("token_daily", "day", "2026-09-{:02d}"), ("token_hourly", "hour", "2026-09-{:02d}T{:02d}")):
        if table == "token_hourly" and not hourly:
            continue
        seen = set()
        while len(seen) < n:
            stamp = fmt.format(rnd.randint(1, 28), rnd.randint(0, 23)) if key == "hour" else fmt.format(rnd.randint(1, 28))
            k = (stamp, rnd.randint(1, 2), rnd.choice(("claude-code", "codex-cli")), rnd.choice(("acme-web", "api", "")),
                 rnd.choice(("model-a", "model-b", "gpt-a")), rnd.choice(("cli", "app", "auto", "")),
                 rnd.choice(("events", "backfill")))
            if k in seen:
                continue
            seen.add(k)
            c.execute(f"INSERT INTO {table} ({key}, device_id, agent, project, model, frontend, source, {', '.join(SUM_COLS)})"
                      " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (*k, rnd.randint(0, 10 ** 12), rnd.randint(0, 10 ** 9), rnd.randint(0, 10 ** 13),
                       rnd.randint(0, 10 ** 8), rnd.randint(0, 10 ** 6), rnd.randint(0, 500)))
    c.commit()
    return {t: totals(c, t) for t in ("token_daily", "token_hourly") if t in tables(c)}


def ledger_rows_without_account(c, table):
    cols = [col for col in columns(c, table) if col != "account"]
    return sorted(tuple(r) for r in c.execute(f"SELECT {', '.join(cols)} FROM {table}"))


def snapshot(c):
    """원장 두 테이블의 모양(열·PK·DDL)과 전체 행 — 롤백 증명에 쓴다."""
    out = {"tables": sorted(tables(c))}
    for t in ("token_daily", "token_hourly"):
        if t in tables(c):
            out[t] = {"cols": columns(c, t), "pk": pk_columns(c, t),
                      "rows": sorted(tuple(r) for r in c.execute(f"SELECT * FROM {t}")),
                      "ddl": c.execute("SELECT sql FROM sqlite_master WHERE name=?", (t,)).fetchone()[0]}
    out["indexes"] = sorted(r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_token%'"))
    return out


class LedgerMigrationTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = str(Path(self.dir) / "t.sqlite")
        self.conn = self.connect()
        self.addCleanup(self.conn.close)

    def connect(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def assert_lock_free(self):
        other = self.connect()
        try:
            other.execute("BEGIN IMMEDIATE")   # 쓰기 락이 남아 있으면 timeout 후 실패
            other.rollback()
        finally:
            other.close()


class RebuildMigrationTests(LedgerMigrationTestCase):
    def test_pk_gains_account_and_every_row_and_sum_is_preserved(self):
        expected = seed_legacy(self.conn)
        before = {t: ledger_rows_without_account(self.conn, t) for t in expected}
        self.assertNotIn("account", columns(self.conn, "token_daily"))
        db.migrate(self.conn)
        for table, key in (("token_daily", "day"), ("token_hourly", "hour")):
            with self.subTest(table=table):
                self.assertEqual(pk_columns(self.conn, table), (key, *NEW_PK))
                self.assertEqual(totals(self.conn, table), expected[table])                      # 행 수와 6개 합계
                self.assertEqual(ledger_rows_without_account(self.conn, table), before[table])   # 행 하나하나
                self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE account != ''").fetchone()[0], 0)
                self.assertEqual(columns(self.conn, table)[:4], (key, "device_id", "agent", "account"))
        self.assertFalse({t for t in tables(self.conn) if t.endswith("__acct")})
        self.assertIn("idx_token_daily_day", {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")})
        self.assertIn("account", columns(self.conn, "sessions"))
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_new_pk_is_enforced_per_account(self):
        seed_legacy(self.conn, n=5)
        db.migrate(self.conn)
        row = self.conn.execute("SELECT * FROM token_daily LIMIT 1").fetchone()
        vals = dict(row)

        def insert(account):
            cols = {**vals, "account": account}
            self.conn.execute(f"INSERT INTO token_daily ({', '.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                              tuple(cols.values()))
        insert("a@example.com")
        insert("b@example.com")                   # 다른 계정은 같은 나머지 키로도 들어간다
        with self.assertRaises(sqlite3.IntegrityError):
            insert("a@example.com")               # 같은 계정은 PK 충돌
        self.conn.rollback()

    def test_ledger_writes_after_migration_accumulate_per_account(self):
        seed_legacy(self.conn, n=3)
        db.migrate(self.conn)
        self.conn.execute("DELETE FROM token_daily")
        self.conn.execute("DELETE FROM token_hourly")
        for acct in ("a@example.com", "a@example.com", "b@example.com", ""):
            tokens.add_ledger(self.conn, "2026-11-26T10", 1, "claude-code", "p", "m", "cli", "events",
                              {"in": 5, "out": 1, "cr": 0, "cw": 0, "th": 0}, turns=1, account=acct)
        for t in ("token_daily", "token_hourly"):
            self.assertEqual({r["account"]: r["input"] for r in self.conn.execute(f"SELECT * FROM {t}")},
                             {"a@example.com": 10, "b@example.com": 5, "": 5})

    def test_migrating_again_changes_nothing_and_rebuilds_nothing(self):
        seed_legacy(self.conn)
        db.migrate(self.conn)
        once = snapshot(self.conn)
        self.assertFalse(db._rebuild_ledger(self.conn, "token_daily", "day"))
        self.assertFalse(db._rebuild_ledger(self.conn, "token_hourly", "hour"))
        with self.assertNoLogs("madison.db", "INFO"):
            db.migrate(self.conn)
            db.migrate(self.conn)
        self.assertEqual(snapshot(self.conn), once)

    def test_rebuild_logs_row_counts_once(self):
        seed_legacy(self.conn, n=10)
        with self.assertLogs("madison.db", "INFO") as logs:
            db.migrate(self.conn)
        msgs = [m for m in logs.output if "원장 재구성" in m]
        self.assertEqual(len(msgs), 2)
        self.assertTrue(any("token_daily" in m and "10행" in m for m in msgs), msgs)
        self.assertTrue(any("token_hourly" in m and "10행" in m for m in msgs), msgs)

    def test_leftover_temp_table_from_an_old_crash_is_discarded(self):
        expected = seed_legacy(self.conn, n=20)
        self.conn.execute(old_ledger_ddl("token_daily__acct", "day"))
        self.conn.execute("INSERT INTO token_daily__acct VALUES ('x',1,'a','p','m','f','events',9,9,9,9,9,9)")
        self.conn.commit()
        db.migrate(self.conn)
        self.assertEqual(totals(self.conn, "token_daily"), expected["token_daily"])
        self.assertNotIn("token_daily__acct", tables(self.conn))

    def test_worker_project_merge_runs_after_the_rebuild(self):
        """merge_worker_projects는 새 PK(account 포함)의 ON CONFLICT를 쓴다 — 재구성 뒤에 돌아야 하고, 합계는 보존된다."""
        seed_legacy(self.conn, n=3)
        for table, key, stamp in (("token_daily", "day", "2026-09-30"), ("token_hourly", "hour", "2026-09-30T10")):
            for project, n in (("llm-cwd", 10), ("summarizer", 5), ("madison", 100)):
                self.conn.execute(
                    f"INSERT INTO {table} ({key}, device_id, agent, project, model, frontend, source, input)"
                    " VALUES (?,1,'claude-code',?,'haiku','auto','events',?)", (stamp, project, n))
        self.conn.commit()
        before = {t: totals(self.conn, t) for t in ("token_daily", "token_hourly")}
        db.migrate(self.conn)
        for table, key, stamp in (("token_daily", "day", "2026-09-30"), ("token_hourly", "hour", "2026-09-30T10")):
            rows = self.conn.execute(f"SELECT project, account, input FROM {table} WHERE {key}=? AND model='haiku'", (stamp,)).fetchall()
            self.assertEqual([tuple(r) for r in rows], [("madison", "", 115)])
            self.assertEqual(totals(self.conn, table)[2:], before[table][2:])    # 합쳐도 열 합계는 같다
            self.assertEqual(totals(self.conn, table)[0], before[table][0] - 2)  # 행만 둘 줄었다

    def test_fresh_database_gets_the_new_shape_directly(self):
        db.migrate(self.conn)
        for table, key in (("token_daily", "day"), ("token_hourly", "hour")):
            self.assertEqual(pk_columns(self.conn, table), (key, *NEW_PK))
            self.assertEqual(totals(self.conn, table), (0,) * 7)
        self.assertEqual(columns(self.conn, "token_daily")[1:], columns(self.conn, "token_hourly")[1:])
        default = [r for r in self.conn.execute("PRAGMA table_info(token_daily)") if r[1] == "account"][0]
        self.assertEqual((default[2], default[3], default[4]), ("TEXT", 1, "''"))
        self.assertEqual(columns(self.conn, "sessions").count("account"), 1)
        self.assertIn("idx_token_daily_day", {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")})
        self.assertEqual(snapshot(self.conn)["tables"].count("token_hourly"), 1)

    def test_ledger_ddl_is_the_single_template_for_schema_and_migration(self):
        mem = sqlite3.connect(":memory:")
        mem.executescript(db.SCHEMA)
        for table, key in (("token_daily", "day"), ("token_hourly", "hour")):
            mem.execute(db._ledger_ddl(f"{table}_probe", key))
            self.assertEqual(columns(mem, table), columns(mem, f"{table}_probe"))
            self.assertEqual(pk_columns(mem, table), pk_columns(mem, f"{table}_probe"))
        mem.close()

    def test_database_without_hourly_ledger_gets_it_created_and_filled_with_session_accounts(self):
        # 새 코드로 사람 세션 이벤트를 받은 뒤, 시간 원장이 없고 일 원장은 옛 형태였던 DB로 되돌려 놓는다
        db.migrate(self.conn)
        self.conn.execute("INSERT INTO devices (id,name,token_hash,created_at) VALUES (1,'acme-mini','x','t')")
        for i, (sid, acct, n) in enumerate((("s-a", "alice@example.com", 100), ("s-b", "bob@example.com", 40), ("s-n", "", 7))):
            detail = {"frontend": "cli", "model": "model-a", "tokens": {"cum": {"model-a": {"in": n, "out": 1, "cr": 0, "cw": 0, "th": 0}}}}
            if acct:
                detail["account"] = acct
            state.ingest(self.conn, 1, {"agent": "claude-code", "session_id": sid, "event_id": f"e{i}", "event": "turn_done",
                                        "ts": "2026-09-02T10:00:00Z", "project": "acme", "detail": detail})
        self.conn.commit()
        live = {r[0]: r[1] for r in self.conn.execute("SELECT account, SUM(input) FROM token_daily GROUP BY account")}
        self.assertEqual(live, {"alice@example.com": 100, "bob@example.com": 40, "": 7})
        cols = "day, device_id, agent, project, model, frontend, source, input, output, cache_read, cache_write, thinking, turns"
        self.conn.execute("ALTER TABLE token_daily RENAME TO token_daily_new")
        self.conn.execute(old_ledger_ddl("token_daily", "day"))
        self.conn.execute(f"INSERT INTO token_daily ({cols}) SELECT day, device_id, agent, project, model, frontend, source,"
                          " SUM(input), SUM(output), SUM(cache_read), SUM(cache_write), SUM(thinking), SUM(turns)"
                          " FROM token_daily_new GROUP BY day, device_id, agent, project, model, frontend, source")
        self.conn.execute("DROP TABLE token_daily_new")
        self.conn.execute("DROP TABLE token_hourly")
        self.conn.commit()
        daily_total = totals(self.conn, "token_daily")

        db.migrate(self.conn)
        self.assertEqual(pk_columns(self.conn, "token_hourly"), ("hour", *NEW_PK))
        self.assertEqual(pk_columns(self.conn, "token_daily"), ("day", *NEW_PK))
        self.assertEqual({r[0] for r in self.conn.execute("SELECT account FROM token_daily")}, {""})   # 옛 일 원장은 전부 ''
        hourly = {r[0]: r[1] for r in self.conn.execute("SELECT account, SUM(input) FROM token_hourly GROUP BY account")}
        self.assertEqual(hourly, live)                                         # 채우기는 세션의 현재 계정
        self.assertEqual(totals(self.conn, "token_hourly")[1:], daily_total[1:])   # 총합은 일 원장과 같다


class RebuildFailureTests(LedgerMigrationTestCase):
    """어느 단계에서 실패해도 원장은 이전 상태 그대로 — 새 열·임시 테이블·부분 복사가 남지 않는다."""

    def fail_and_assert_untouched(self, patch_ctx, expected_error=RuntimeError, *, hourly=True):
        seed_legacy(self.conn, hourly=hourly, n=60)
        before = snapshot(self.conn)
        with patch_ctx:
            with self.assertRaises(expected_error):
                db.migrate(self.conn)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(snapshot(self.conn), before)
        self.assertFalse({t for t in tables(self.conn) if t.endswith("__acct")})
        self.assertNotIn("account", columns(self.conn, "token_daily"))
        self.assert_lock_free()
        # 같은 DB에서 장애가 사라지면 이어서 성공한다 — 데이터는 그대로 새 형태로
        db.migrate(self.conn)
        self.assertEqual(pk_columns(self.conn, "token_daily"), ("day", *NEW_PK))
        for t in ("token_daily", "token_hourly"):
            if t in before:
                self.assertEqual(totals(self.conn, t), (len(before[t]["rows"]), *[
                    sum(r[7 + i] for r in before[t]["rows"]) for i in range(6)]))

    @staticmethod
    def totals_that_lie(target, how):
        real = db._ledger_totals

        def fake(c, table):
            got = real(c, table)
            if table != target:
                return got
            if how == "rows":
                return (got[0] + 1, *got[1:])
            if how == "sum":
                return (*got[:3], got[3] + 1, *got[4:])
            return got
        return mock.patch.object(db, "_ledger_totals", fake)

    def test_row_count_mismatch_in_daily_rolls_everything_back(self):
        self.fail_and_assert_untouched(self.totals_that_lie("token_daily__acct", "rows"))

    def test_sum_mismatch_in_daily_rolls_everything_back(self):
        self.fail_and_assert_untouched(self.totals_that_lie("token_daily__acct", "sum"))

    def test_mismatch_in_hourly_also_undoes_the_already_rebuilt_daily(self):
        """일 원장 재구성이 끝난 뒤 시간 원장에서 실패해도 같은 트랜잭션이라 일 원장도 이전 모양으로 돌아간다."""
        self.fail_and_assert_untouched(self.totals_that_lie("token_hourly__acct", "sum"))

    def test_failure_after_both_rebuilds_undoes_both(self):
        self.fail_and_assert_untouched(mock.patch.object(tokens, "merge_worker_projects", side_effect=RuntimeError("boom")))

    def test_failure_while_copying_leaves_the_original_intact(self):
        real = db._ledger_ddl

        def broken(name, key):
            # 시드의 turns는 0..500 — CHECK에 걸리는 행에서 복사가 도중에 실패한다
            return real(name, key).replace("turns INTEGER NOT NULL DEFAULT 0,", "turns INTEGER NOT NULL DEFAULT 0 CHECK (turns < 400),")
        self.fail_and_assert_untouched(mock.patch.object(db, "_ledger_ddl", broken), sqlite3.IntegrityError)

    def test_failure_in_hourly_fill_when_the_hourly_ledger_is_missing(self):
        self.fail_and_assert_untouched(mock.patch.object(tokens, "backfill_hourly", side_effect=RuntimeError("fill")),
                                       hourly=False)

    def test_keyboard_interrupt_style_failure_also_rolls_back(self):
        self.fail_and_assert_untouched(mock.patch.object(tokens, "merge_worker_projects", side_effect=KeyboardInterrupt),
                                       KeyboardInterrupt)

    def test_validation_message_names_the_table_and_both_tuples(self):
        seed_legacy(self.conn, n=5)
        with self.totals_that_lie("token_daily__acct", "rows"):
            with self.assertRaisesRegex(RuntimeError, r"token_daily.*불일치.*이전.*이후"):
                db.migrate(self.conn)


class ConcurrentStartupTests(LedgerMigrationTestCase):
    def test_second_process_waits_for_the_write_lock_and_then_does_not_rebuild(self):
        expected = seed_legacy(self.conn, n=120)
        before = {t: ledger_rows_without_account(self.conn, t) for t in expected}
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []
        real_rebuild, real_totals = db._rebuild_ledger, db._ledger_totals
        first = []

        def rebuild(c, table, key):
            out = real_rebuild(c, table, key)
            results.append((threading.current_thread().name, table, out))
            return out

        def totals_hook(c, table):
            if table == "token_daily__acct" and not first:     # A가 재구성 한가운데(락을 쥔 채)에서 멈춘다
                first.append(threading.current_thread().name)
                entered.set()
                self.assertTrue(release.wait(20))
            return real_totals(c, table)

        def run(name):
            c = self.connect()
            try:
                db.migrate(c)
            except BaseException as e:   # noqa: BLE001 — 스레드 안 실패를 메인에서 단언
                errors.append((name, e))
            finally:
                c.close()

        with mock.patch.object(db, "_rebuild_ledger", rebuild), mock.patch.object(db, "_ledger_totals", totals_hook):
            ta = threading.Thread(target=run, args=("A",), name="A")
            ta.start()
            self.assertTrue(entered.wait(20))
            # MIGRATIONS의 ALTER가 B를 우연히 먼저 막는 일이 없게 비운다(A는 이미 지나갔다)
            with mock.patch.object(db, "MIGRATIONS", ()):
                tb = threading.Thread(target=run, args=("B",), name="B")
                tb.start()
                time.sleep(0.5)
                self.assertTrue(tb.is_alive(), "B는 A의 쓰기 락이 풀릴 때까지 기다려야 한다")
                release.set()
                ta.join(30)
                tb.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), sorted([("A", "token_daily", True), ("A", "token_hourly", True),
                                                  ("B", "token_daily", False), ("B", "token_hourly", False)]))
        for t in expected:   # 이중 처리·유실 없음
            self.assertEqual(totals(self.conn, t), expected[t])
            self.assertEqual(ledger_rows_without_account(self.conn, t), before[t])
            self.assertEqual(pk_columns(self.conn, t)[3], "account")

    def test_many_simultaneous_starters_rebuild_exactly_once(self):
        expected = seed_legacy(self.conn, n=150)
        n = 5
        barrier = threading.Barrier(n)
        results, errors = [], []
        real_rebuild = db._rebuild_ledger

        def rebuild(c, table, key):
            out = real_rebuild(c, table, key)
            results.append((table, out))
            return out

        def run():
            c = self.connect()
            try:
                barrier.wait(20)
                db.migrate(c)
            except BaseException as e:   # noqa: BLE001
                errors.append(e)
            finally:
                c.close()

        with mock.patch.object(db, "_rebuild_ledger", rebuild):
            threads = [threading.Thread(target=run) for _ in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(sum(1 for t, out in results if t == "token_daily" and out), 1)
        self.assertEqual(sum(1 for t, out in results if t == "token_hourly" and out), 1)
        for t in expected:
            self.assertEqual(totals(self.conn, t), expected[t])

    def test_concurrent_starters_on_a_database_without_hourly_fill_once(self):
        seed_legacy(self.conn, hourly=False, n=50)
        barrier = threading.Barrier(3)
        errors = []
        fills = []
        real_fill = tokens.backfill_hourly

        def fill(c):
            fills.append(1)
            return real_fill(c)

        def run():
            c = self.connect()
            try:
                barrier.wait(20)
                db.migrate(c)
            except BaseException as e:   # noqa: BLE001
                errors.append(e)
            finally:
                c.close()
        with mock.patch.object(tokens, "backfill_hourly", fill):
            threads = [threading.Thread(target=run) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(len(fills), 1)
        self.assertEqual(pk_columns(self.conn, "token_hourly")[3], "account")


class ConnPathTests(unittest.TestCase):
    """db.conn() — 기동 경로 그대로(임시 경로의 파일 DB)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = Path(self.dir) / "data" / "t.sqlite"
        self.path.parent.mkdir()
        patcher = mock.patch.object(db, "CFG", SimpleNamespace(db_path=self.path))
        patcher.start()
        self.addCleanup(patcher.stop)
        self._saved = db._conn
        db._conn = None
        self.addCleanup(self.restore)

    def restore(self):
        if db._conn is not None:
            db._conn.close()
        db._conn = self._saved

    def make_legacy_file(self, **kw):
        c = sqlite3.connect(str(self.path))
        c.row_factory = sqlite3.Row
        expected = seed_legacy(c, **kw)
        c.close()
        return expected

    def test_conn_migrates_a_legacy_file_and_tx_works_afterwards(self):
        expected = self.make_legacy_file(n=80)
        c = db.conn()
        self.assertIs(db.conn(), c)
        for t in expected:
            self.assertEqual(totals(c, t), expected[t])
            self.assertEqual(pk_columns(c, t)[3], "account")
        with db.tx() as cc:
            tokens.add_ledger(cc, "2026-11-26T10", 1, "claude-code", "p", "m", "cli", "events",
                              {"in": 3, "out": 0, "cr": 0, "cw": 0, "th": 0}, account="a@example.com")
        self.assertEqual(c.execute("SELECT account, input FROM token_daily WHERE project='p'").fetchone()[:], ("a@example.com", 3))

    def test_failed_migration_does_not_publish_the_connection_and_the_file_is_untouched(self):
        expected = self.make_legacy_file(n=40)
        raw = sqlite3.connect(str(self.path))
        raw.row_factory = sqlite3.Row
        before = snapshot(raw)
        raw.close()
        with mock.patch.object(tokens, "merge_worker_projects", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                db.conn()
        self.assertIsNone(db._conn)
        raw = sqlite3.connect(str(self.path))
        raw.row_factory = sqlite3.Row
        self.assertEqual(snapshot(raw), before)
        raw.close()
        c = db.conn()   # 다음 기동에서 다시 시도되어 성공
        for t in expected:
            self.assertEqual(totals(c, t), expected[t])


class AccountsModuleTests(unittest.TestCase):
    def setUp(self):
        accounts.clear_worker_cache()
        self.addCleanup(accounts.clear_worker_cache)
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)

    def write_claude(self, email="Dev@Example.com", org_type="claude_max"):
        (self.home / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"emailAddress": email, "organizationType": org_type}}))

    def write_codex(self, email="Codex@Example.com"):
        seg = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
        (self.home / ".codex").mkdir(exist_ok=True)
        (self.home / ".codex" / "auth.json").write_text(json.dumps(
            {"auth_mode": "chatgpt", "tokens": {"id_token": f"h.{seg}.s"}}))

    def test_rule_module_is_loaded_once_from_the_collector_file(self):
        mod = accounts._rule()
        self.assertIs(accounts._rule(), mod)
        self.assertEqual(Path(mod.__file__), REPO_ROOT / "collector" / "account.py")

    def test_reexports_follow_the_collector_rule(self):
        mod = accounts._rule()
        for v in ("Dev@Example.com", " a b@example.com", "api:Acme  Org", "api:", "nope", None, 5):
            self.assertEqual(accounts.normalize(v), mod.normalize(v))
        self.assertEqual(accounts.normalize("Dev@Example.com"), "dev@example.com")
        self.assertTrue(accounts.is_subscription("dev@example.com"))
        self.assertFalse(accounts.is_subscription("api:abc"))
        self.assertFalse(accounts.is_subscription(""))
        self.assertTrue(accounts.is_api("api:abc"))
        self.assertFalse(accounts.is_api("dev@example.com"))
        self.write_claude()
        self.assertEqual(accounts.resolve("claude-code", {}, home=self.home), "dev@example.com")

    def test_worker_account_uses_process_env_as_headless_and_caches_for_the_ttl(self):
        key = "sk-fake-worker-key"
        clock = [1000.0]
        with mock.patch.object(accounts.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(Path, "home", return_value=self.home), \
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": key}, clear=True):
            first = accounts.worker_account("claude-code")
            self.assertEqual(first, "api:" + accounts._rule().sha8(key))
            os.environ["ANTHROPIC_API_KEY"] = "sk-fake-other-key"          # TTL 안에서는 다시 읽지 않는다
            clock[0] += accounts.WORKER_TTL - 1
            self.assertEqual(accounts.worker_account("claude-code"), first)
            clock[0] += 2                                                    # TTL 경과 → 다시 해석
            self.assertEqual(accounts.worker_account("claude-code"), "api:" + accounts._rule().sha8("sk-fake-other-key"))

    def test_worker_account_is_cached_per_agent_and_ttl_is_300_seconds(self):
        self.assertEqual(accounts.WORKER_TTL, 300)
        self.write_claude()
        self.write_codex()
        with mock.patch.object(Path, "home", return_value=self.home), mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(accounts.worker_account("claude-code"), "dev@example.com")
            self.assertEqual(accounts.worker_account("codex-cli"), "codex@example.com")

    def test_worker_account_headless_ignores_the_interactive_rejection_list(self):
        """대화형에서 거절한 API 키도 헤드리스 워커는 그대로 쓴다 — 워커 계정은 headless=True로 구한다."""
        key = "sk-fake-rejected-key-0123456789"
        rule = accounts._rule()
        (self.home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "dev@example.com", "organizationType": "claude_max"},
            "customApiKeyResponses": {"rejected": [key[-20:]]}}))
        with mock.patch.object(Path, "home", return_value=self.home), \
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": key}, clear=True):
            self.assertEqual(rule.resolve("claude-code", dict(os.environ), home=self.home), "dev@example.com")   # 대화형
            self.assertEqual(accounts.worker_account("claude-code"), "api:" + rule.sha8(key))                    # 워커

    def test_worker_account_never_raises_and_unknown_inputs_are_empty(self):
        with mock.patch.object(accounts, "resolve", side_effect=RuntimeError("boom")):
            self.assertEqual(accounts.worker_account("claude-code"), "")
        with mock.patch.object(Path, "home", return_value=self.home), mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(accounts.worker_account("unknown-agent"), "")
            self.assertEqual(accounts.worker_account("codex-cli"), "")


if __name__ == "__main__":
    unittest.main()
