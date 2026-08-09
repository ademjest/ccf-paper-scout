import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ccf_paper_scout import cli


class CliTests(unittest.TestCase):
    def test_help_returns_zero(self):
        with self.assertRaises(SystemExit) as raised:
            cli.main(["--help"])
        self.assertEqual(raised.exception.code, 0)

    def test_run_forwards_remainder_to_legacy_cli(self):
        with mock.patch("paper_scout.main", return_value=7) as legacy, \
             mock.patch.object(sys, "argv", ["ccf-paper-scout"]):
            result = cli.main(["run", "--config", "foo.json", "--no-update-seen"])
        self.assertEqual(result, 7)
        legacy.assert_called_once()

    def test_run_preserves_legacy_argument_order(self):
        captured = []

        def legacy_main():
            captured.extend(sys.argv[1:])
            return 0

        with mock.patch("paper_scout.main", side_effect=legacy_main), \
             mock.patch.object(sys, "argv", ["ccf-paper-scout"]):
            result = cli.main(["run", "--config", "foo.json", "--output", "out.md", "--no-update-seen"])
        self.assertEqual(result, 0)
        config_index = captured.index("--config")
        self.assertEqual(captured[config_index + 1], "foo.json")
        output_index = captured.index("--output")
        self.assertEqual(captured[output_index + 1], "out.md")

    def test_doctor_checks_configured_relative_state_db_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = root / "config.json"
            path.write_text(json.dumps({
                "years": [2026], "venue_keys": ["nips"], "max_results": 5,
                "state_db": "custom/state.sqlite3"
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {"ZOTERO_USER_ID": "1", "ZOTERO_API_KEY": "k"}, clear=True), \
                 mock.patch("builtins.print") as output:
                code = cli.main(["doctor", "--config", str(path), "--no-network"])
            text = " ".join(" ".join(map(str, call.args)) for call in output.call_args_list)
            self.assertEqual(code, 0)
            self.assertIn(str(root / "custom"), text)
            self.assertTrue((root / "custom").is_dir())

    def test_doctor_reports_missing_credentials_without_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "config.json"
            path.write_text(json.dumps({
                "years": [2026], "venue_keys": ["nips"], "max_results": 5,
                "delivery": {"smtp": {"enabled": True, "host": "smtp.qq.com", "port": 465,
                    "sender": "a@qq.com", "receiver": "b@example.com", "password_env": "SMTP_PASSWORD"}}
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch("builtins.print") as output:
                code = cli.main(["doctor", "--config", str(path), "--no-network"])
            text = " ".join(" ".join(map(str, call.args)) for call in output.call_args_list)
            self.assertEqual(code, 2)
            self.assertIn("SMTP_PASSWORD: missing", text)
            self.assertNotIn("secret", text)


if __name__ == "__main__":
    unittest.main()
