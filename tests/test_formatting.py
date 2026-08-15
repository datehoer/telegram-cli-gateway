from __future__ import annotations

import unittest

from cli_telegram_gateway.formatting import markdown_to_telegram_html, split_markdown


class TelegramMarkdownTests(unittest.TestCase):
    def test_renders_supported_markdown_and_escapes_html(self) -> None:
        source = "# Title\n**bold** *italic* ~~gone~~ `x < y`\n- item\n<unsafe>"
        rendered = markdown_to_telegram_html(source)
        self.assertIn("<b>Title</b>", rendered)
        self.assertIn("<b>bold</b>", rendered)
        self.assertIn("<i>italic</i>", rendered)
        self.assertIn("<s>gone</s>", rendered)
        self.assertIn("<code>x &lt; y</code>", rendered)
        self.assertIn("• item", rendered)
        self.assertIn("&lt;unsafe&gt;", rendered)

    def test_renders_safe_links_and_does_not_activate_unsafe_links(self) -> None:
        rendered = markdown_to_telegram_html(
            "[safe](https://example.com/?a=1&b=2) [bad](javascript:alert)"
        )
        self.assertIn('<a href="https://example.com/?a=1&amp;b=2">safe</a>', rendered)
        self.assertNotIn("href=\"javascript:", rendered)
        self.assertIn("[bad](javascript:alert)", rendered)

    def test_unclosed_streaming_code_fence_still_produces_balanced_html(self) -> None:
        rendered = markdown_to_telegram_html("```python\nprint('<ok>')")
        self.assertEqual(
            rendered,
            '<pre><code class="language-python">print(\'&lt;ok&gt;\')</code></pre>',
        )

    def test_long_fenced_code_is_balanced_across_chunks(self) -> None:
        chunks = split_markdown("```text\n" + "x" * 100 + "\n```", limit=45)
        self.assertGreater(len(chunks), 2)
        for chunk in chunks:
            rendered = markdown_to_telegram_html(chunk)
            self.assertEqual(rendered.count("<pre>"), rendered.count("</pre>"))


if __name__ == "__main__":
    unittest.main()
