"""Real spawned-process SQLite contention tests; only temporary databases used."""
import contextlib
import multiprocessing
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path


def _database(path, timeout_ms):
    # database.py has a legacy import-time singleton. Point it at an isolated
    # sibling before import in EVERY fresh spawned interpreter.
    os.environ['FLIPPER_DB_PATH'] = str(Path(path).with_name(f'bootstrap-{os.getpid()}.db'))
    from auto_flipper.database import Database
    return Database(path, busy_timeout_ms=timeout_ms)


def _claim_worker(path, ready, start, result, lot):
    try:
        db = _database(path, 2500)
        ready.set()
        if not start.wait(10):
            raise TimeoutError('Test start not signalled')
        result.put(('ok', db.claim_purchase(lot, 'cs2', 50, True)))
    except Exception as error:
        result.put(('error', type(error).__name__, str(error)))


def _hold_write(path, ready, release, exclusive=False):
    with contextlib.closing(sqlite3.connect(path, timeout=2)) as conn:
        conn.execute('BEGIN EXCLUSIVE' if exclusive else 'BEGIN IMMEDIATE')
        conn.execute("INSERT INTO flipper_logs(action,details,timestamp) VALUES('holder','uncommitted','test')")
        ready.set()
        if not release.wait(10):
            raise TimeoutError('Test release not signalled')
        conn.commit()


def _waiting_claim(path, ready, start, attempted, result):
    try:
        db = _database(path, 2500)
        ready.set()
        if not start.wait(10):
            raise TimeoutError('Test start not signalled')
        attempted.set()
        began = time.monotonic()
        claim = db.claim_purchase('waited-lot', 'cs2', 50, True)
        result.put(('ok', claim, time.monotonic()-began))
    except Exception as error:
        result.put(('error', type(error).__name__, str(error)))


class DatabaseConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='flipper-process-checks-')
        self.path = str(Path(self.temp.name)/'flipper.db')
        self.db = _database(self.path, 200)
        self.ctx = multiprocessing.get_context('spawn')
        self.processes = []
        self.release_events = []

    def tearDown(self):
        for event in self.release_events:
            event.set()
        for process in self.processes:
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)
        self.temp.cleanup()

    def start_process(self, target, args):
        process = self.ctx.Process(target=target, args=args)
        process.start()
        self.processes.append(process)
        return process

    def hold_write(self):
        ready, release = self.ctx.Event(), self.ctx.Event()
        self.release_events.append(release)
        process = self.start_process(_hold_write, (self.path, ready, release))
        self.assertTrue(ready.wait(5), 'Writer process did not acquire lock')
        return process, release

    def test_wal_and_busy_timeout_are_explicit(self):
        with self.db._get_connection() as conn:
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            self.assertEqual(conn.execute('PRAGMA busy_timeout').fetchone()[0], 200)
            self.assertEqual(conn.execute('PRAGMA synchronous').fetchone()[0], 2)
        for timeout in (True, 0, 30001, 1.5):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                _database(self.path, timeout)

    def test_two_processes_can_create_only_one_unresolved_claim(self):
        result, start = self.ctx.Queue(), self.ctx.Event()
        ready = [self.ctx.Event(), self.ctx.Event()]
        workers = [self.start_process(_claim_worker,
                   (self.path, ready[index], start, result, f'lot-{index}'))
                   for index in range(2)]
        self.assertTrue(all(event.wait(10) for event in ready))
        start.set()
        outcomes = [result.get(timeout=10), result.get(timeout=10)]
        self.assertTrue(all(item[0] == 'ok' for item in outcomes), outcomes)
        self.assertEqual(sum(item[1] is not None for item in outcomes), 1, outcomes)
        for worker in workers:
            worker.join(5)
            self.assertEqual(worker.exitcode, 0)
        with self.db._get_connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM purchase_intents').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM purchase_claims').fetchone()[0], 1)

    def test_waiting_writer_succeeds_after_other_process_commits(self):
        ready, start, attempted = self.ctx.Event(), self.ctx.Event(), self.ctx.Event()
        result = self.ctx.Queue()
        worker = self.start_process(_waiting_claim, (self.path, ready, start, attempted, result))
        self.assertTrue(ready.wait(10))
        holder, release = self.hold_write()
        start.set()
        self.assertTrue(attempted.wait(5))
        # Hold long enough to exercise SQLite's busy handler in a different process.
        time.sleep(.20)
        release.set()
        outcome = result.get(timeout=10)
        self.assertEqual(outcome[0], 'ok', outcome)
        self.assertIsNotNone(outcome[1])
        self.assertGreaterEqual(outcome[2], .15)
        self.assertLess(outcome[2], 3.5)
        for process in (worker, holder):
            process.join(5)
            self.assertEqual(process.exitcode, 0)

    def test_lock_timeout_is_bounded_and_leaves_no_partial_claim(self):
        from auto_flipper.database import DatabaseBusyError
        holder, release = self.hold_write()
        began = time.monotonic()
        with self.assertRaises(DatabaseBusyError):
            self.db.claim_purchase('blocked-lot', 'cs2', 50, True)
        elapsed = time.monotonic()-began
        self.assertGreaterEqual(elapsed, .15)
        self.assertLess(elapsed, 1.5)
        # WAL permits committed-state readers while the other writer still holds.
        with self.db._get_connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM purchase_intents').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM purchase_claims').fetchone()[0], 0)
        release.set()
        holder.join(5)
        self.assertEqual(holder.exitcode, 0)
        # Explicitly new action after reconciliation works; no hidden automatic retry.
        self.assertIsNotNone(self.db.claim_purchase('blocked-lot', 'cs2', 50, True))

    def test_reader_gets_committed_state_during_process_write(self):
        holder, release = self.hold_write()
        began = time.monotonic()
        with self.db._get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM flipper_logs WHERE action='holder'").fetchone()[0], 0)
        self.assertLess(time.monotonic()-began, .5)
        release.set()
        holder.join(5)
        with self.db._get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM flipper_logs WHERE action='holder'").fetchone()[0], 1)

    def test_read_snapshot_upgrade_failure_rolls_back_before_new_action(self):
        from auto_flipper.database import DatabaseBusyError
        with self.assertRaises(DatabaseBusyError):
            with self.db._get_connection() as conn:
                conn.execute('BEGIN')
                conn.execute('SELECT COUNT(*) FROM purchase_intents').fetchone()
                # Another process commits after this connection's read snapshot.
                holder, release = self.hold_write()
                release.set()
                holder.join(5)
                self.assertEqual(holder.exitcode, 0)
                conn.execute("INSERT INTO purchase_claims VALUES('stale',1,'stale-intent')")
        with self.db._get_connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM purchase_claims').fetchone()[0], 0)
        self.assertIsNotNone(self.db.claim_purchase('fresh-lot', 'cs2', 50, True))

    def test_exception_after_partial_writes_rolls_back_all_intent_records(self):
        with self.assertRaises(RuntimeError):
            with self.db._get_connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute("INSERT INTO purchase_claims VALUES('failed',1,'failed-intent')")
                raise RuntimeError('Injected failure after first local insert')
        with self.db._get_connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM purchase_claims').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM purchase_intents').fetchone()[0], 0)

    def test_legacy_startup_lock_is_bounded_and_backup_not_left_partial(self):
        from auto_flipper.database import DatabaseBusyError
        legacy = str(Path(self.temp.name)/'legacy.db')
        with contextlib.closing(sqlite3.connect(legacy)) as conn:
            conn.execute('CREATE TABLE flipper_logs(action TEXT,details TEXT,timestamp TEXT)')
            conn.commit()
        ready, release = self.ctx.Event(), self.ctx.Event()
        self.release_events.append(release)
        holder = self.start_process(_hold_write, (legacy, ready, release, True))
        self.assertTrue(ready.wait(5))
        began = time.monotonic()
        with self.assertRaises(DatabaseBusyError):
            _database(legacy, 200)
        self.assertLess(time.monotonic()-began, 1.5)
        self.assertFalse(Path(legacy+'.pre_safe_v1.bak').exists())
        self.assertFalse(list(Path(self.temp.name).glob('*.tmp-*')))
        release.set()
        holder.join(5)


if __name__ == '__main__':
    unittest.main()
