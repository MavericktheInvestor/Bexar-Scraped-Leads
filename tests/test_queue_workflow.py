"""Check the actual producer workflow's admission predicate (no Actions writes)."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class QueueWorkflowTests(unittest.TestCase):
    def test_queue_only_follows_successful_main_scrapes_from_this_repository(self):
        workflow = yaml.load(
            (ROOT / '.github/workflows/skip_trace_queue.yml').read_text(encoding='utf-8'),
            Loader=yaml.BaseLoader,
        )
        job = workflow['jobs']['build-queue']
        expression = job.get('if', 'True').strip()
        expression = expression.removeprefix('${{').removesuffix('}}').strip()
        expression = expression.replace('&&', ' and ').replace('||', ' or ')
        tree = ast.parse(expression, mode='eval')
        for node in ast.walk(tree):
            self.assertIsInstance(node, (
                ast.Expression, ast.BoolOp, ast.Compare, ast.Attribute,
                ast.Name, ast.Constant, ast.And, ast.Or, ast.Eq, ast.Load,
            ))
            if isinstance(node, ast.Attribute):
                self.assertFalse(node.attr.startswith('_'))
            if isinstance(node, ast.Name):
                self.assertEqual(node.id, 'github')
        predicate = compile(tree, '<queue job condition>', 'eval')
        repo = 'MavericktheInvestor/Bexar-Scraped-Leads'
        for event, conclusion, branch, source_repo, allowed in [
            ('workflow_dispatch', '', '', '', True),
            ('workflow_run', 'success', 'main', repo, True),
            ('workflow_run', 'failure', 'main', repo, False),
            ('workflow_run', 'cancelled', 'main', repo, False),
            ('workflow_run', 'success', 'feature', repo, False),
            ('workflow_run', 'success', 'main', 'other/fork', False),
        ]:
            with self.subTest(event=event, conclusion=conclusion, branch=branch, source_repo=source_repo):
                github = SimpleNamespace(
                    event_name=event, repository=repo,
                    event=SimpleNamespace(workflow_run=SimpleNamespace(
                        conclusion=conclusion, head_branch=branch,
                        head_repository=SimpleNamespace(full_name=source_repo),
                    )),
                )
                self.assertEqual(bool(eval(predicate, {'__builtins__': {}}, {'github': github})), allowed)
        checkout = next(s for s in job['steps'] if s.get('uses', '').startswith('actions/checkout@'))
        self.assertEqual(checkout['with'].get('ref'), 'main')


if __name__ == '__main__':
    unittest.main()
