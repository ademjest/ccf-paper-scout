"""Regression coverage for finalizers after a failed private-state restore."""
from pathlib import Path
import re
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1] / '.github' / 'workflows'


def step_field(workflow, name, field):
    # Read the actual named step without adding a YAML dependency to the base CLI.
    block = workflow.split('      - name: ' + name + '\n', 1)[1].split('\n      - ', 1)[0]
    return re.search(r'^        ' + field + r': (.+)$', block, re.MULTILINE)[1]


class StatePersistenceTests(unittest.TestCase):
    def should_persist(self, filename, name, outcome, mode, job_success):
        workflow = (ROOT / filename).read_text(encoding='utf-8')
        identifier = step_field(workflow, 'Restore private state', 'id')
        expression = step_field(workflow, name, 'if')
        # Evaluate the boolean subset used by these workflow conditions, including
        # Actions' implicit success guard when no explicit status function is present.
        if not re.search(r'\b(always|success|failure|cancelled)\(', expression) and not job_success:
            return False
        context = dict(always=lambda: True, success=lambda: job_success,
                       steps=SimpleNamespace(**{identifier: SimpleNamespace(outcome=outcome)}),
                       inputs=SimpleNamespace(mode=mode))
        return eval(expression.replace('&&', ' and ').replace('||', ' or '), {'__builtins__': {}}, context)

    def test_failed_or_skipped_restore_never_enters_missing_directory(self):
        for filename, name in [('paper-scout-daily.yml', 'Persist state immediately after delivery'),
                               ('paper-scout-manual.yml', 'Persist production state')]:
            for outcome in ('failure', 'skipped', 'cancelled'):
                with self.subTest(workflow=filename, outcome=outcome):
                    self.assertFalse(self.should_persist(filename, name, outcome, 'production', False))

    def test_restored_state_is_saved_even_after_delivery_failure(self):
        for success in (True, False):
            self.assertTrue(self.should_persist('paper-scout-daily.yml', 'Persist state immediately after delivery',
                                                'success', '', success))
            for mode in ('production', 'discover'):
                self.assertTrue(self.should_persist('paper-scout-manual.yml', 'Persist production state',
                                                    'success', mode, success))

    def test_manual_read_only_and_stateless_modes_do_not_persist_production_state(self):
        for mode in ('preview', 'evaluate', 'doctor', 'smtp-test', 'research'):
            with self.subTest(mode=mode):
                self.assertFalse(self.should_persist('paper-scout-manual.yml', 'Persist production state',
                                                     'success', mode, True))


if __name__ == '__main__':
    unittest.main()
