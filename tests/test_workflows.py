import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def read(self, name):
        return (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")

    def test_manual_workflow_is_safe_by_default(self):
        text = self.read("paper-scout-manual.yml")
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("default: doctor", text)
        self.assertIn("timeout-minutes: 60", text)
        self.assertIn("group: paper-scout-production", text)
        self.assertIn("--no-update-seen", text)
        self.assertNotIn("printenv", text)
        self.assertNotIn("cat config.action.json", text)
        self.assertNotIn("x-access-token:${STATE_REPO_TOKEN}@", text)
        self.assertIn("extraheader", text)
        self.assertIn(".delivery-pending", text)
        self.assertIn("actions_state.py verify", text)
        self.assertNotIn('"${{ inputs.max_results }}"', text)
        self.assertIn("REQUESTED_MAX_RESULTS", text)
        self.assertNotIn("contents: write", text)

    def test_daily_workflow_is_short_beijing_schedule(self):
        text = self.read("paper-scout-daily.yml")
        self.assertIn("cron: '0 1 * * *'", text)
        self.assertIn("timeout-minutes: 60", text)
        self.assertIn("group: paper-scout-production", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertNotIn("sleep 20400", text)
        self.assertNotIn("workflow_dispatches", text)
        self.assertNotIn("x-access-token:${STATE_REPO_TOKEN}@", text)
        self.assertIn("extraheader", text)
        self.assertIn(".delivery-pending", text)
        self.assertIn("actions_state.py verify", text)

    def test_keep_alive_has_no_business_secrets(self):
        text = self.read("keep-alive.yml")
        self.assertIn("contents: write", text)
        self.assertIn("[skip ci]", text)
        for name in ("ZOTERO", "SMTP_PASSWORD", "LLM_API_KEY", "STATE_REPO_TOKEN"):
            self.assertNotIn(name, text)
        self.assertNotIn("--force", text)


if __name__ == "__main__":
    unittest.main()
