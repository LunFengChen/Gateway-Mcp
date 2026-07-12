import unittest
from types import SimpleNamespace

from gateway_mcp_server import _extract_result


class ExtractResultTests(unittest.TestCase):
    def test_prefers_structured_content_over_generic_text(self) -> None:
        result = SimpleNamespace(
            structured_content={"id": 1466, "url": "https://example.test"},
            content=[SimpleNamespace(text="Successfully retrieved the record.")],
        )

        self.assertEqual(
            _extract_result(result),
            {"id": 1466, "url": "https://example.test"},
        )

    def test_falls_back_to_text_content(self) -> None:
        result = SimpleNamespace(
            structured_content=None,
            content=[SimpleNamespace(text="plain text result")],
        )

        self.assertEqual(_extract_result(result), "plain text result")


if __name__ == "__main__":
    unittest.main()
