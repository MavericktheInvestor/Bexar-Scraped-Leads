from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]


class SheetWorkflowTests(unittest.TestCase):
    def test_dedicated_serial_readonly_workflow_has_safe_manual_and_success_gated_apply(self):
        path = ROOT/'.github/workflows/subtrst_sheet_sync.yml'
        self.assertTrue(path.exists(), 'Dedicated sync workflow is missing')
        workflow = yaml.safe_load(path.read_text(encoding='utf-8'))
        events = workflow.get('on', workflow.get(True))
        mode = events['workflow_dispatch']['inputs']['mode']
        self.assertEqual(mode['default'], 'audit')
        self.assertEqual(mode['type'], 'choice')
        self.assertEqual(mode['options'], ['audit', 'apply'])
        self.assertEqual(events['workflow_run']['workflows'], ['Scrape Bexar County Leads'])
        self.assertEqual(events['workflow_run']['types'], ['completed'])
        self.assertEqual(events['workflow_run']['branches'], ['main'])
        self.assertEqual(len(events['schedule']), 1)
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        self.assertFalse(workflow['concurrency']['cancel-in-progress'])
        job = workflow['jobs']['sync']
        for fragment in ["conclusion == 'success'", "head_branch == 'main'", 'head_repository.full_name == github.repository']:
            self.assertIn(fragment, job['if'])
        steps = job['steps']
        checkout = next(s for s in steps if s.get('uses', '').startswith('actions/checkout@'))
        self.assertEqual(checkout['with']['ref'], 'main')
        self.assertFalse(checkout['with']['persist-credentials'])
        python = next(s for s in steps if s.get('uses', '').startswith('actions/setup-python@'))
        self.assertEqual(python['with']['python-version'], '3.11')
        run = next(s for s in steps if 'python scraper/subtrst_sheet_sync.py' in s.get('run', ''))
        self.assertEqual(run['env']['SUBTRST_SOURCE'], 'data/subtrst_records.json')
        self.assertIn("'workflow_dispatch'", run['env']['SUBTRST_SYNC_MODE'])
        self.assertIn("'apply'", run['env']['SUBTRST_SYNC_MODE'])
        self.assertEqual(run['env']['GOOGLE_CREDENTIALS'], '${{ secrets.GOOGLE_CREDENTIALS }}')
        self.assertFalse(any('ghl_' in s.get('run', '') or 'skip_trace' in s.get('run', '') for s in steps))
        self.assertTrue(any('google-api-python-client' in s.get('run', '') for s in steps))

    def test_main_scraper_commit_includes_cumulative_archive(self):
        workflow = yaml.safe_load((ROOT/'.github/workflows/main.yml').read_text(encoding='utf-8'))
        commit = next(s['run'] for s in workflow['jobs']['scrape']['steps']
                      if s.get('name') == 'Commit and push results')
        add_lines = '\n'.join(line for line in commit.splitlines() if 'git add ' in line)
        self.assertIn('git add data/subtrst_records.json', add_lines)
