import unittest

from src.extract import extract_appids_from_search_html


class TestExtractAppidsFromSearchHtml(unittest.TestCase):
    def test_handles_single_and_list_values(self):
        """Ensure app IDs are extracted from both single and list-style HTML values."""
        html = (
            '<div data-ds-appid="123"></div>'
            '<div data-ds-appid="[456, 789]"></div>'
            '<div data-ds-appid="abc"></div>'
        )

        self.assertEqual(extract_appids_from_search_html(html), [123, 456, 789])