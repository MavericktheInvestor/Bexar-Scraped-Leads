"""Regression checks for the scraper-to-public-dashboard publishing path."""
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]


class PagesWorkflowTests(unittest.TestCase):
    def test_successful_scrape_has_explicit_dashboard_deployment(self):
        path = ROOT / '.github/workflows/pages.yml'
        self.assertTrue(path.exists(), 'No Pages deployment workflow: scraper updates never reach the website')
        workflow = yaml.load(path.read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
        triggers = workflow['on']
        self.assertIn('Scrape Bexar County Leads', triggers['workflow_run']['workflows'])
        self.assertIn('completed', triggers['workflow_run']['types'])
        self.assertIn('workflow_dispatch', triggers)
        self.assertIn('push', triggers)
        job = workflow['jobs']['deploy']
        self.assertIn('build', workflow['jobs'], 'Artifact upload must finish in a separate job before deployment')
        build = workflow['jobs']['build']
        self.assertEqual(job['needs'], 'build')
        self.assertIn("github.event.workflow_run.conclusion == 'success'", build['if'])
        self.assertIn("github.event.workflow_run.head_branch == 'main'", build['if'])
        self.assertEqual(job['permissions']['contents'], 'read')
        self.assertEqual(job['permissions']['pages'], 'write')
        self.assertEqual(job['permissions']['id-token'], 'write')
        self.assertEqual(job['environment']['name'], 'github-pages')
        steps = build['steps']
        validation = next(s['run'] for s in steps if s.get('name') == 'Validate dashboard data')
        self.assertIn("data.get('scrape_complete') is True", validation)
        checkout = next(s for s in steps if s.get('uses', '').startswith('actions/checkout@'))
        self.assertEqual(checkout['with']['ref'], 'main')
        upload = next(s for s in steps if s.get('uses', '').startswith('actions/upload-pages-artifact@'))
        self.assertEqual(upload['with']['path'], 'dashboard/')
        self.assertTrue(any(s.get('uses', '').startswith('actions/deploy-pages@') for s in job['steps']))
        self.assertFalse(any('scraper/fetch.py' in s.get('run', '') or 'ghl_push' in s.get('run', '') for s in steps))


if __name__ == '__main__':
    unittest.main()
