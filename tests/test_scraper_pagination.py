"""Exercise the real scraper functions without importing their side effects."""
import ast
import asyncio
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import re
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


SOURCE = Path(__file__).resolve().parents[1] / "scraper" / "fetch.py"


def load_function(name, namespace):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace[name]


async def no_sleep(_):
    pass


class Button:
    def __init__(self, page):
        self.page = page

    async def is_visible(self):
        return True

    async def get_attribute(self, name):
        return None

    async def click(self):
        if self.page.navigation_error:
            raise RuntimeError("navigation failed")
        self.page.number += 1
        self.page.visited.append(self.page.number)


class DateInput:
    def __init__(self):
        self.value = ''

    async def is_visible(self):
        return True

    async def is_editable(self):
        return True

    async def click(self):
        pass

    async def fill(self, value):
        self.value = value


class SearchButton:
    async def is_visible(self):
        return True

    async def is_enabled(self) -> bool:
        return True

    async def click(self):
        pass


class Page:
    def __init__(self, total, navigation_error=False, repeat=False, selector_error=False, loading=False, empty=False, hidden_loading=False):
        self.total = total
        self.number = 1
        self.visited = [1]
        self.navigation_error = navigation_error
        self.repeat = repeat
        self.selector_error = selector_error
        self.loading = loading
        self.empty = empty
        self.hidden_loading = hidden_loading
        self.dates = [DateInput(), DateInput()]
        self.keyboard = types.SimpleNamespace(press=no_sleep)

    def on(self, *_):
        pass

    async def goto(self, *args, **kwargs):
        pass

    async def wait_for_load_state(self, *args, **kwargs):
        pass

    async def title(self):
        return "Fixture portal"

    async def content(self):
        if self.number > 1 and self.empty:
            return ""
        if self.number > 1 and self.loading:
            return "loading results"
        return str(1 if self.repeat else self.number) + ('<script>loading</script>' if self.hidden_loading else '')

    async def inner_text(self, selector):
        return (await self.content()).split('<')[0]

    def get_by_role(self, *args, **kwargs):
        return types.SimpleNamespace(count=self.zero)

    async def zero(self):
        return 0

    async def query_selector_all(self, *_):
        return self.dates

    async def query_selector(self, selector):
        if selector == "button[type='submit']":
            return SearchButton()
        if self.selector_error and "next" in selector:
            raise RuntimeError("selector failed")
        if "aria-label*='next'" in selector and self.number < self.total:
            return Button(self)
        return None


class Browser:
    def __init__(self, page):
        self.page = page
        self.closed = False

    async def new_context(self, **kwargs):
        return self

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True

    async def launch(self, **kwargs):
        return self

    async def __aenter__(self):
        return types.SimpleNamespace(chromium=self)

    async def __aexit__(self, *args):
        pass


def scrape(page):
    browser = Browser(page)
    module = types.ModuleType("playwright.async_api")
    module.async_playwright = lambda: browser
    namespace = {
        "asyncio": types.SimpleNamespace(sleep=no_sleep), "re": re,
        "BeautifulSoup": lambda *_: types.SimpleNamespace(find_all=lambda *_: []),
        "CLERK_BASE": "https://fixture.invalid", "PROXY_USER": "", "PROXY_PASS": "",
        "parse_html": lambda html, **kwargs: [{"doc_num": html.split('<')[0]}] if html else [],
        "_api_row": lambda row: row, "_items": lambda body: body,
    }
    function = load_function("playwright_scrape", namespace)
    with patch.dict(sys.modules, {"playwright": types.ModuleType("playwright"), "playwright.async_api": module}), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        try:
            return asyncio.run(function("01/01/2026", "01/07/2026", "2026-01-01", None, None))
        finally:
            if not browser.closed:
                raise AssertionError("browser was not closed")


class MainHarness:
    def __init__(self, result=None, error=None):
        self.writes = []
        self.exports = []
        self.calls = []
        destination = MagicMock()
        destination.__truediv__.return_value.write_text.side_effect = self.writes.append

        async def scraper(*args):
            self.calls.append(args)
            if error:
                raise error
            if callable(result):
                return result(len(self.calls))
            return result or []

        self.namespace = {
            "datetime": types.SimpleNamespace(utcnow=lambda: datetime(2026, 1, 7), strptime=datetime.strptime),
            "timedelta": timedelta,
            "re": re, "json": json, "safe": lambda v: str(v).strip() if v else "",
            "date_range_mm": lambda: ("01/01/2026", "01/07/2026"),
            "date_range_iso": lambda: ("2026-01-01", "2026-01-07"),
            "DOC_TYPE_MAP": {"NOFC": "Foreclosure"},
            "find_working_proxy": lambda: ("first", "1"),
            "PROXY_LIST": [("first", "1"), ("second", "2")],
            "ParcelLookup": lambda **_: types.SimpleNamespace(lookup=lambda _: {}),
            "playwright_scrape": scraper,
            "score_record": lambda *_: ([], 70),
            "DASH_DIR": destination, "DATA_DIR": destination,
            "export_csv": lambda *args: self.exports.append("csv"),
            "push_to_sheets": lambda *args: self.exports.append("sheets"),
        }

    def run(self):
        with redirect_stdout(io.StringIO()):
            return asyncio.run(load_function("main", self.namespace)())


class MainCompletionTests(unittest.TestCase):
    def test_parcel_enrichment_preserves_filing_address_and_fills_missing_fields(self):
        harness = MainHarness(result=[{
            'doc_num': '1', 'doc_type': 'NOFC', 'owner': 'Jane Doe',
            'filed': '2026-01-02', 'is_motivated': True,
            'prop_address': '123 FILING ST', 'mail_address': 'N/A', 'mail_state': 'TX',
        }])
        harness.namespace['ParcelLookup'] = lambda **_: types.SimpleNamespace(lookup=lambda _: {
            'prop_address': '456 DIFFERENT PARCEL ST', 'mail_address': 'PO BOX 7',
            'mail_city': 'OAKLAND', 'mail_state': 'CA', 'mail_zip': '94601'})
        harness.run()
        result = json.loads(harness.writes[0])['records'][0]
        self.assertEqual(result['prop_address'], '123 FILING ST')
        self.assertEqual(result['mail_address'], 'PO BOX 7')
        self.assertEqual(result['mail_state'], 'CA')

    def test_enrichment_only_requests_accepted_target_owners(self):
        harness = MainHarness(result=[
            {'doc_num': '1', 'doc_type': 'NOFC', 'owner': 'Jane Doe',
             'filed': '2026-01-02', 'is_motivated': True},
            {'doc_num': '2', 'doc_type': 'OTHER', 'owner': 'Other Name',
             'filed': '2026-01-02', 'is_motivated': False},
        ])
        requested = []
        def lookup_factory(owners=()):
            requested.extend(owners)
            return types.SimpleNamespace(lookup=lambda _: {})
        harness.namespace['ParcelLookup'] = lookup_factory
        harness.run()
        self.assertEqual(requested, ['Jane Doe'])

    def test_failed_collection_does_not_request_parcel_data(self):
        harness = MainHarness(error=RuntimeError('county access blocked'))
        parcel_requests = []
        harness.namespace['ParcelLookup'] = lambda: parcel_requests.append(True)
        with self.assertRaisesRegex(RuntimeError, 'county access blocked'):
            harness.run()
        self.assertEqual(parcel_requests, [])
        self.assertEqual(harness.writes, [])

    def test_loading_failure_retries_whole_window_with_next_proxy(self):
        def result(call):
            if call == 1:
                raise RuntimeError('Pagination page 57 is still loading')
            return [{
                'doc_num': '123456', 'doc_type': 'NOFC', 'owner': 'Jane Doe',
                'filed': '2026-01-02', 'is_motivated': True,
            }]
        harness = MainHarness(result=result)
        harness.run()
        self.assertEqual([c[3:] for c in harness.calls], [('first', '1'), ('second', '2')])
        self.assertEqual(harness.calls[0][:3], harness.calls[1][:3])
        self.assertEqual(json.loads(harness.writes[0])['total'], 1)
        self.assertEqual(harness.exports, ['csv', 'sheets'])

    def test_large_lookback_is_collected_in_seven_day_windows(self):
        harness = MainHarness(result=[{
            "doc_num": "123456", "doc_type": "NOFC", "owner": "Jane Doe",
            "filed": "2026-01-02", "is_motivated": True,
        }])
        harness.namespace['date_range_iso'] = lambda: ('2026-01-01', '2026-01-16')
        harness.run()
        self.assertEqual([(c[0], c[1], c[2]) for c in harness.calls], [
            ('01/01/2026', '01/07/2026', '2026-01-01'),
            ('01/08/2026', '01/14/2026', '2026-01-08'),
            ('01/15/2026', '01/16/2026', '2026-01-15'),
        ])
        self.assertEqual(json.loads(harness.writes[0])['total'], 1)
        self.assertEqual(json.loads(harness.writes[0])['data_range'], '2026-01-01 to 2026-01-16')

    def test_failed_later_window_does_not_export_earlier_windows(self):
        def result(call):
            if call > 1:
                raise RuntimeError('later window incomplete')
            return [{'doc_num': '1'}]
        harness = MainHarness(result=result)
        harness.namespace['date_range_iso'] = lambda: ('2026-01-01', '2026-01-16')
        with self.assertRaisesRegex(RuntimeError, 'later window incomplete'):
            harness.run()
        self.assertEqual([c[2] for c in harness.calls], ['2026-01-01', '2026-01-08', '2026-01-08'])
        self.assertEqual(harness.writes, [])
        self.assertEqual(harness.exports, [])

    def test_all_sources_empty_raises_before_any_exports(self):
        harness = MainHarness()
        with self.assertRaisesRegex(RuntimeError, "No complete scrape"):
            harness.run()
        self.assertEqual(len(harness.calls), 2)
        self.assertEqual(harness.writes, [])
        self.assertEqual(harness.exports, [])

    def test_successful_export_marks_scrape_complete(self):
        harness = MainHarness(result=[{
            "doc_num": "123456", "doc_type": "NOFC", "owner": "Jane Doe",
            "filed": "2026-01-02", "is_motivated": True,
        }])
        harness.run()
        self.assertEqual(len(harness.writes), 2)
        for written in harness.writes:
            payload = json.loads(written)
            self.assertIs(payload.get("scrape_complete"), True)
            self.assertEqual(payload["total"], 1)
        self.assertEqual(harness.exports, ["csv", "sheets"])

    def test_scraper_exception_prevents_any_exports(self):
        harness = MainHarness(error=RuntimeError("pagination incomplete"))
        with self.assertRaisesRegex(RuntimeError, "pagination incomplete"):
            harness.run()
        self.assertEqual(harness.writes, [])
        self.assertEqual(harness.exports, [])


class PaginationTests(unittest.TestCase):
    def test_disappearing_date_controls_fail_before_unbounded_search(self):
        class DisappearingPage(Page):
            reads = 0

            async def query_selector_all(self, *_):
                self.reads += 1
                return self.dates if self.reads == 1 else []

        with self.assertRaisesRegex(RuntimeError, 'date controls disappeared'):
            scrape(DisappearingPage(1))

    def test_missing_date_controls_never_collect_unbounded_rows(self):
        class MissingDatesPage(Page):
            async def query_selector_all(self, *_):
                return []

        with self.assertRaisesRegex(RuntimeError, 'search form not ready'):
            scrape(MissingDatesPage(1))

    def test_disabled_search_never_collects_stale_rows(self):
        class DisabledSearch(SearchButton):
            async def is_enabled(self):
                return False

        class DisabledPage(Page):
            async def query_selector(self, selector):
                if selector == "button[type='submit']":
                    return DisabledSearch()
                return await super().query_selector(selector)

        with self.assertRaisesRegex(RuntimeError, 'search form not ready'):
            scrape(DisabledPage(1))

    def test_waits_for_delayed_form_render(self):
        class DelayedPage(Page):
            reads = 0

            async def query_selector_all(self, *_):
                self.reads += 1
                return self.dates if self.reads >= 3 else []

        page = DelayedPage(1)
        self.assertEqual(scrape(page), [{'doc_num': '1'}])
        self.assertGreaterEqual(page.reads, 3)

    def test_hidden_security_script_does_not_block_ready_form(self):
        class HiddenSecurityPage(Page):
            async def content(self):
                return (await super().content()) + '<script>Performing security verification</script>'

        self.assertEqual(scrape(HiddenSecurityPage(1)), [{'doc_num': '1'}])

    def test_failed_search_submission_does_not_collect_stale_rows(self):
        class FailingSearch(SearchButton):
            async def click(self):
                raise RuntimeError('search submission failed')

        class FailedSubmitPage(Page):
            async def query_selector(self, selector):
                if selector == "button[type='submit']":
                    return FailingSearch()
                return await super().query_selector(selector)

        with self.assertRaisesRegex(RuntimeError, 'search submission failed'):
            scrape(FailedSubmitPage(1))

    def test_security_verification_is_reported_as_access_blocker(self):
        class BlockedPage(Page):
            async def inner_text(self, selector):
                return 'Performing security verification. This website uses a security service to protect against malicious bots.'

            async def query_selector_all(self, *_):
                return []

        with self.assertRaisesRegex(RuntimeError, 'access blocked.*security verification'):
            scrape(BlockedPage(1))

    def test_ready_form_collects_when_network_never_becomes_idle(self):
        class BusyPage(Page):
            async def wait_for_load_state(self, *args, **kwargs):
                raise TimeoutError('background traffic never became idle')

        page = BusyPage(6)
        self.assertEqual([r['doc_num'] for r in scrape(page)], [str(n) for n in range(1, 7)])
        self.assertEqual([d.value for d in page.dates], ['01/01/2026', '01/07/2026'])

    def test_script_loading_strings_do_not_block_loaded_results(self):
        page = Page(6, hidden_loading=True)
        self.assertEqual([r['doc_num'] for r in scrape(page)], [str(n) for n in range(1, 7)])

    def test_visits_page_six_instead_of_returning_partial_results(self):
        page = Page(6)
        records = scrape(page)
        self.assertEqual(page.visited, list(range(1, 7)))
        self.assertEqual([r["doc_num"] for r in records], [str(n) for n in range(1, 7)])

    def test_cap_with_next_raises_instead_of_returning_partial_results(self):
        page = Page(2001)
        with self.assertRaisesRegex(RuntimeError, "2000.*Next"):
            scrape(page)
        self.assertEqual(page.visited[-1], 2000)

    def test_repeated_document_page_raises(self):
        page = Page(3, repeat=True)
        with self.assertRaisesRegex(RuntimeError, "Repeated document page"):
            scrape(page)
        self.assertEqual(page.visited, [1, 2])

    def test_navigation_error_raises_instead_of_returning_first_page(self):
        with self.assertRaisesRegex(RuntimeError, "navigation failed"):
            scrape(Page(2, navigation_error=True))

    def test_next_detection_error_raises_instead_of_claiming_completion(self):
        with self.assertRaisesRegex(RuntimeError, "selector failed"):
            scrape(Page(2, selector_error=True))

    def test_loading_timeout_raises_instead_of_claiming_completion(self):
        with self.assertRaisesRegex(RuntimeError, "loading"):
            scrape(Page(2, loading=True))

    def test_empty_navigation_result_is_not_treated_as_last_page(self):
        with self.assertRaisesRegex(RuntimeError, "No documents"):
            scrape(Page(2, empty=True))

    def test_no_next_stops_on_first_page(self):
        page = Page(1)
        self.assertEqual(scrape(page), [{"doc_num": "1"}])
        self.assertEqual(page.visited, [1])

    def test_last_page_exactly_at_cap_is_accepted(self):
        page = Page(2000)
        self.assertEqual(len(scrape(page)), 2000)
        self.assertEqual(page.visited[-1], 2000)


if __name__ == "__main__":
    unittest.main()
