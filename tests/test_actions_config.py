import json
import os
import pathlib
import stat
import tempfile
import unittest
from unittest import mock

from scripts import build_actions_config


class ActionsConfigTests(unittest.TestCase):
    def test_build_writes_private_cloud_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            base = root / "base.json"
            out = root / "config.action.json"
            base.write_text(json.dumps({
                "years": [2026], "venue_keys": ["nips"], "max_results": 20,
                "llm_translation": {"enabled": False, "cache": "state/translations.json"},
                "delivery": {"smtp": {"enabled": False}},
                "seen_db": "state/seen.json", "state_db": "state/paper_scout.sqlite3",
                "run_lock": "state/paper_scout.lock"
            }), encoding="utf-8")
            env = {"SMTP_SENDER": "sender@qq.com", "SMTP_RECEIVER": "receiver@example.com",
                   "LLM_BASE_URL": "https://llm.example/v1", "LLM_MODEL": "model-x"}
            with mock.patch.dict(os.environ, env, clear=True):
                build_actions_config.build(base, out, pathlib.Path(".runtime-state/state"), 5)
            payload = json.loads(out.read_text())
            self.assertEqual(payload["max_results"], 5)
            self.assertEqual(payload["state_db"], ".runtime-state/state/paper_scout.sqlite3")
            self.assertEqual(payload["seen_db"], ".runtime-state/state/seen.json")
            self.assertTrue(payload["delivery"]["smtp"]["enabled"])
            self.assertEqual(payload["delivery"]["smtp"]["host"], "smtp.qq.com")
            self.assertEqual(payload["llm_translation"]["model"], "model-x")
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
            text = out.read_text()
            self.assertNotIn("sender-password-value", text)
            self.assertNotIn("llm-api-key-value", text)

    def test_build_rejects_out_of_range_results(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory) / "base.json"
            base.write_text(json.dumps({"years": [2026], "venue_keys": []}), encoding="utf-8")
            env = {"SMTP_SENDER": "s@qq.com", "SMTP_RECEIVER": "r@example.com",
                   "LLM_BASE_URL": "https://llm.example/v1", "LLM_MODEL": "m"}
            with mock.patch.dict(os.environ, env, clear=True):
                for value in (-1, 0, 21):
                    with self.assertRaisesRegex(ValueError, "1..20"):
                        build_actions_config.build(base, pathlib.Path(directory) / "out.json", pathlib.Path("state"), value)

    def test_build_fails_when_public_runtime_values_are_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory) / "base.json"
            base.write_text(json.dumps({"years": [2026], "venue_keys": []}), encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "missing Actions configuration"):
                    build_actions_config.build(base, pathlib.Path(directory) / "out.json", pathlib.Path("state"), 5)


if __name__ == "__main__":
    unittest.main()
