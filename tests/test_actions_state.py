import pathlib
import sqlite3
import tempfile
import unittest

from scripts import actions_state


class ActionsStateTests(unittest.TestCase):
    def test_prepare_removes_runtime_files_and_keeps_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "paper_scout.lock").write_text("lock")
            (root / "paper_scout.sqlite3-wal").write_text("wal")
            (root / "seen.json").write_text("{}")
            actions_state.prepare(root)
            self.assertFalse((root / "paper_scout.lock").exists())
            self.assertFalse((root / "paper_scout.sqlite3-wal").exists())
            self.assertTrue((root / "seen.json").exists())

    def test_checkpoint_sqlite_and_reject_unknown_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            db = root / "paper_scout.sqlite3"
            connection = sqlite3.connect(db)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE x(v INTEGER)")
            connection.execute("INSERT INTO x VALUES (1)")
            connection.commit(); connection.close()
            actions_state.checkpoint(root)
            self.assertEqual(sqlite3.connect(db).execute("PRAGMA integrity_check").fetchone()[0], "ok")
            (root / "secret.txt").write_text("no")
            with self.assertRaisesRegex(RuntimeError, "unexpected state entries"):
                actions_state.verify(root)
            (root / "secret.txt").unlink()
            (root / "nested").mkdir()
            (root / "nested" / "secret.txt").write_text("no")
            with self.assertRaisesRegex(RuntimeError, "unexpected state entries"):
                actions_state.verify(root)


if __name__ == "__main__":
    unittest.main()
