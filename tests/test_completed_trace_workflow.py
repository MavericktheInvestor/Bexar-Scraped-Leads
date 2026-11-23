import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def test_readonly_default_full_history_correct_success_gates_and_sheets_only_secret(self):
        text = (ROOT / '.github/workflows/completed_trace_sheet_sync.yml').read_text()
        self.assertIn('default: audit', text)
        self.assertIn('options: [audit, apply]', text)
        self.assertIn('schedule:', text)
        self.assertIn('Skip Trace Leads (Deal Machine)', text)
        self.assertIn('Build Skip Trace Queue', text)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", text)
        self.assertIn('head_repository.full_name == github.repository', text)
        self.assertIn('ref: main', text)
        self.assertIn('fetch-depth: 0', text)
        self.assertIn('persist-credentials: false', text)
        self.assertEqual({'GOOGLE_CREDENTIALS'}, set(re.findall(r'secrets\.([A-Z_]+)', text)))
        self.assertIn('python -m unittest discover -s tests -p "test_completed_trace*.py" -v', text)
        self.assertIn('actions/upload-artifact@v4', text)
        self.assertIn('if: always()', text)
        self.assertNotIn('deal_machine_skip_trace.py', text)

    def test_automatic_apply_requires_explicit_repository_gate_manual_mode_remains_explicit(self):
        text = (ROOT / '.github/workflows/completed_trace_sheet_sync.yml').read_text()
        expression = re.search(r'SYNC_MODE: \$\{\{ (.+) \}\}', text).group(1)
        expression = expression.replace('&&', ' and ').replace('||', ' or ')
        for event in ['schedule', 'workflow_run', 'workflow_dispatch']:
            for gate in ['', 'false', 'true', 'not-enabled']:
                for mode in ['', 'audit', 'apply']:
                    with self.subTest(event=event, gate=gate, mode=mode):
                        rendered = expression.replace('github.event_name', repr(event))
                        rendered = rendered.replace('inputs.mode', repr(mode))
                        rendered = rendered.replace('vars.COMPLETED_TRACE_MASTER_APPLY_ENABLED', repr(gate))
                        actual = eval(rendered, {'__builtins__': {}}, {})
                        expected = (mode or 'audit') if event == 'workflow_dispatch' else (
                            'apply' if gate == 'true' else 'audit')
                        self.assertEqual(expected, actual)


if __name__ == '__main__':
    unittest.main()
