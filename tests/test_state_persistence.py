"""State recovery tests with real SQLite snapshots and a local Git remote."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ccf_paper_scout import delivery_receipts as receipts
from scripts import actions_state as state
from state_store import StateStore


def receipt():
    return dict(version=1, run_id='delivery-one', actions_run_id='123', accepted_at='2026-11-09T01:00:00+00:00',
                selected_paper_ids=['conf/icml/test'], report_sha256=hashlib.sha256(b'report').hexdigest(),
                paper_aliases={'conf/icml/test': [['doi', '10.1234/example']]}, evidence='smtp_accepted')


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'state'
        self.root.mkdir()
        self.db = self.root / state.DB_NAME
        self.store = StateStore(self.db)
        self.store.close()

    def test_legacy_roundtrip_and_deterministic_compression(self):
        state.restore(self.root)
        info = state.pack(self.root)
        self.assertEqual(state.pack(self.root), info)
        restored = self.root.parent / 'restored'
        restored.mkdir()
        for name in ('snapshot.json', state.DB_NAME + '.gz'):
            shutil.copyfile(self.root / name, restored / name)
        state.restore(restored)
        self.assertEqual(state.digest(restored / state.DB_NAME), info['raw_sha256'])

    def test_live_committed_wal_is_included_without_deletion(self):
        writer = sqlite3.connect(self.db)
        try:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('INSERT INTO papers VALUES (?,?,?,?)', ('paper-in-wal', 'title', '', 'now'))
            writer.commit()
            wal = Path(str(self.db) + '-wal')
            self.assertGreater(wal.stat().st_size, 0)
            state.prepare(self.root)
            self.assertTrue(wal.exists())
            state.pack(self.root)
            restored = self.root.parent / 'restored'
            restored.mkdir()
            for name in ('snapshot.json', state.DB_NAME + '.gz'):
                shutil.copyfile(self.root / name, restored / name)
            state.restore(restored)
            con = sqlite3.connect(restored / state.DB_NAME)
            try:
                self.assertEqual(con.execute('SELECT paper_id FROM papers').fetchone()[0], 'paper-in-wal')
            finally:
                con.close()
        finally:
            writer.close()

    def test_corrupt_archive_does_not_create_database(self):
        state.pack(self.root)
        self.db.unlink()
        archive = self.root / (state.DB_NAME + '.gz')
        archive.write_bytes(archive.read_bytes()[:-8])
        with self.assertRaisesRegex(RuntimeError, 'checksum'):
            state.restore(self.root)
        self.assertFalse(self.db.exists())

    def test_ambiguous_or_missing_state_is_not_silently_reset(self):
        state.pack(self.root)
        with self.assertRaisesRegex(RuntimeError, 'ambiguous'):
            state.restore(self.root)
        for name in (state.DB_NAME, state.DB_NAME + '.gz', 'snapshot.json'):
            (self.root / name).unlink()
        (self.root / 'seen.json').write_text('{"ids": ["old"]}')
        with self.assertRaisesRegex(RuntimeError, 'missing'):
            state.restore(self.root)

    def test_budget_failure_preserves_previous_archive(self):
        state.pack(self.root)
        before = (self.root / 'snapshot.json').read_bytes()
        with patch.object(state, 'MAX_BYTES', 1), self.assertRaisesRegex(RuntimeError, 'upload budget'):
            state.pack(self.root)
        self.assertEqual((self.root / 'snapshot.json').read_bytes(), before)

    def test_pending_without_evidence_remains_blocked(self):
        marker = dict(run_id='delivery-one', phase='delivery_pending', selected_paper_ids=['conf/icml/test'],
                      report_sha256=receipt()['report_sha256'])
        receipts.atomic_json(self.root / '.delivery-pending', marker)
        state.reconcile(self.root)
        self.assertTrue((self.root / '.delivery-pending').exists())
        self.assertFalse((self.root / receipts.FILENAME).exists())

    def test_accepted_marker_recovers_when_receipt_write_was_interrupted(self):
        r = receipt()
        marker = dict(r, phase='smtp_accepted', updated_at=r['accepted_at'])
        receipts.atomic_json(self.root / '.delivery-pending', marker)
        state.reconcile(self.root)
        self.assertFalse((self.root / '.delivery-pending').exists())
        self.assertEqual(receipts.load(self.root)['delivery-one'], r)

    def test_receipt_replay_without_original_run_is_idempotent(self):
        r = receipt()
        receipts.record(self.root, r)
        receipts.atomic_json(self.root / 'seen.json', {'ids': ['older-paper']})
        state.reconcile(self.root)
        state.reconcile(self.root)
        store = StateStore(self.db)
        try:
            self.assertIn('conf/icml/test', store.delivered_ids())
            self.assertEqual(store.connection.execute('SELECT count(*) FROM delivery_receipts').fetchone()[0], 1)
            self.assertEqual(store.connection.execute('SELECT count(*) FROM recommendation_runs').fetchone()[0], 0)
            self.assertEqual(store.connection.execute('SELECT external_id FROM paper_aliases WHERE namespace="doi"').fetchone()[0], '10.1234/example')
        finally:
            store.close()
        self.assertEqual(set(json.loads((self.root / 'seen.json').read_text())['ids']), {'older-paper', 'conf/icml/test'})

    def test_conflicting_receipt_never_clears_pending(self):
        r = receipt()
        receipts.record(self.root, r)
        changed = copy.deepcopy(r)
        changed['selected_paper_ids'] = ['different']
        changed['paper_aliases'] = {}
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            receipts.record(self.root, changed)
        receipts.atomic_json(self.root / '.delivery-pending', dict(changed, phase='delivery_pending'))
        with self.assertRaisesRegex(ValueError, 'does not match'):
            state.reconcile(self.root)
        self.assertTrue((self.root / '.delivery-pending').exists())


@unittest.skipUnless(shutil.which('git'), 'Git is required for persistence integration')
class GitPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.remote = self.base / 'remote.git'
        self.repo = self.base / 'working'
        self.repo.mkdir()
        self.root = self.repo / 'state'
        self.root.mkdir()
        self.command('init', '--bare', '--initial-branch=main', str(self.remote))
        self.command('init', '--initial-branch=main', str(self.repo))
        self.command('-C', str(self.repo), 'config', 'user.name', 'Test')
        self.command('-C', str(self.repo), 'config', 'user.email', 'test@example.invalid')
        store = StateStore(self.root / state.DB_NAME)
        store.close()
        self.command('-C', str(self.repo), 'add', 'state')
        self.command('-C', str(self.repo), 'commit', '-m', 'old state')
        self.command('-C', str(self.repo), 'remote', 'add', 'origin', str(self.remote))
        self.command('-C', str(self.repo), 'push', '-u', 'origin', 'main')

    def command(self, *args):
        return subprocess.run(['git', *args], check=True, capture_output=True, text=True).stdout

    def clone(self):
        target = self.base / 'recovered'
        self.command('clone', str(self.remote), str(target))
        return target / 'state'

    def test_compression_failure_still_leaves_receipt_on_remote(self):
        receipts.record(self.root, receipt())
        with patch.dict(os.environ, {'STATE_REPO_TOKEN': ''}), patch.object(state, 'pack', side_effect=RuntimeError('size limit')):
            with self.assertRaisesRegex(RuntimeError, 'size limit'):
                state.persist(self.root)
        recovered = self.clone()
        self.assertIn('delivery-one', receipts.load(recovered))
        state.restore(recovered)
        store = StateStore(recovered / state.DB_NAME)
        try:
            self.assertIn('conf/icml/test', store.delivered_ids())
        finally:
            store.close()

    def test_migration_tracks_only_compressed_database(self):
        receipts.record(self.root, receipt())
        with patch.dict(os.environ, {'STATE_REPO_TOKEN': ''}):
            state.persist(self.root)
        recovered = self.clone()
        self.assertFalse((recovered / state.DB_NAME).exists())
        self.assertTrue((recovered / (state.DB_NAME + '.gz')).exists())
        state.restore(recovered)
        self.assertEqual(json.loads((recovered / 'seen.json').read_text())['ids'], ['conf/icml/test'])

    def test_large_push_failure_does_not_roll_back_small_remote_commit(self):
        receipts.record(self.root, receipt())
        original = state.git
        pushes = []

        def fail_second_push(repo, *args, **kwargs):
            if args[0] == 'push':
                pushes.append(args)
                if len(pushes) == 2:
                    raise RuntimeError('remote rejected snapshot')
            return original(repo, *args, **kwargs)

        with patch.dict(os.environ, {'STATE_REPO_TOKEN': ''}), patch.object(state, 'git', side_effect=fail_second_push):
            with self.assertRaisesRegex(RuntimeError, 'remote rejected'):
                state.persist(self.root)
        recovered = self.clone()
        self.assertIn('delivery-one', receipts.load(recovered))
        self.assertTrue((recovered / state.DB_NAME).exists())
        self.assertFalse((recovered / (state.DB_NAME + '.gz')).exists())


if __name__ == '__main__':
    unittest.main()
