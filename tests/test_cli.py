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
