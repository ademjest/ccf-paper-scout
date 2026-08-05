import json
import pathlib
import sqlite3
import tempfile
import unittest

import state_store


class StateStoreTests(unittest.TestCase):
    def test_schema_enables_wal_and_foreign_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            store = state_store.StateStore(pathlib.Path(directory) / "state.sqlite3")
            self.assertEqual(store.connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(store.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            store.close()

    def test_local_and_empty_runs_do_not_create_smtp_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = state_store.StateStore(pathlib.Path(directory) / "state.sqlite3")
            local = store.start_run("hash")
            store.finish_run(local, "local_output")
            empty = store.start_run("hash")
            store.finish_run(empty, "no_candidates")
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0], 0)
            statuses = [row[0] for row in store.connection.execute("SELECT status FROM recommendation_runs ORDER BY started_at")]
            self.assertEqual(statuses, ["local_output", "no_candidates"])
            store.close()

    def test_preview_does_not_mark_selected_items_delivered(self):
        with tempfile.TemporaryDirectory() as directory:
            store = state_store.StateStore(pathlib.Path(directory) / "state.sqlite3")
            run_id = store.start_run("hash")
            store.record_selection(run_id, [{"id": "p1", "title": "Paper", "score": 1.0}])
            store.finish_run(run_id, "preview")
            self.assertEqual(store.delivered_ids(), set())
            store.close()

    def test_fail_run_persists_error_and_closes_running_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = state_store.StateStore(pathlib.Path(directory) / "state.sqlite3")
            run_id = store.start_run("hash")
            store.fail_run(run_id, "boom")
            self.assertEqual(store.connection.execute("SELECT status FROM recommendation_runs WHERE run_id=?", (run_id,)).fetchone()[0], "failed")
            store.close()

    def test_delivery_transaction_marks_seen_only_on_success(self):
        with tempfile.TemporaryDirectory() as directory:
            store = state_store.StateStore(pathlib.Path(directory) / "state.sqlite3")
            run_id = store.start_run("hash")
            store.record_selection(run_id, [{"id": "p1", "title": "Paper", "score": 1.0}])
            store.finish_delivery(run_id, False, "smtp rejected")
            self.assertEqual(store.delivered_ids(), set())
            run_id = store.start_run("hash")
            store.record_selection(run_id, [{"id": "p1", "title": "Paper", "score": 1.0}])
            store.finish_delivery(run_id, True, "accepted")
            self.assertEqual(store.delivered_ids(), {"p1"})
            store.close()

    def test_legacy_json_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            seen = root / "seen.json"
            translations = root / "translations.json"
            seen.write_text(json.dumps({"ids": ["p1", "p2"]}), encoding="utf-8")
            translations.write_text(json.dumps({"p1": {"focus": "agent", "_fingerprint": "f1"}}), encoding="utf-8")
            store = state_store.StateStore(root / "state.sqlite3")
            store.migrate_legacy(seen, translations)
            store.migrate_legacy(seen, translations)
            self.assertEqual(store.delivered_ids(), {"p1", "p2"})
            self.assertEqual(store.load_translation("p1", "f1")["focus"], "agent")
            self.assertTrue(list(root.glob("seen.json.*.bak")))
            store.close()

    def test_source_cursor_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = state_store.StateStore(pathlib.Path(directory) / "state.sqlite3")
            store.save_cursor("dblp", "nips", 2026, 200)
            self.assertEqual(store.load_cursor("dblp", "nips", 2026), 200)
            store.close()


if __name__ == "__main__":
    unittest.main()
