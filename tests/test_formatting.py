from __future__ import annotations

import unittest

from cli_telegram_gateway.formatting import markdown_to_telegram_html, split_markdown, stream_segment


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

    def test_stream_segments_preserve_source_and_code_indentation(self) -> None:
        source = "intro\n\n```python\n" + "    print('你好 😀')\n" * 200 + "```\n\ntail"
        offset = 0
        recovered = []
        chunks = []
        while offset < len(source):
            chunk, end = stream_segment(source, offset, 150)
            self.assertGreater(end, offset)
            self.assertLessEqual(len(chunk.encode("utf-16-le")) // 2, 150)
            recovered.append(source[offset:end])
            chunks.append(chunk)
            rendered = markdown_to_telegram_html(chunk)
            self.assertEqual(rendered.count("<pre>"), rendered.count("</pre>"))
            offset = end
        self.assertEqual("".join(recovered), source)
        self.assertTrue(any(chunk.startswith("```python\n    ") for chunk in chunks[1:]))

    def test_stream_segment_counts_emoji_as_two_units(self) -> None:
        source = "😀" * 2000
        chunk, end = stream_segment(source)
        self.assertLessEqual(len(chunk.encode("utf-16-le")) // 2, 1500)
        self.assertEqual(chunk, source[:end])
        self.assertLess(end, 1000)

    def test_empty_tail_does_not_reopen_an_unclosed_code_block(self) -> None:
        source = "```text\nhello"
        self.assertEqual(stream_segment(source, len(source)), ("", len(source)))


if __name__ == "__main__":
    unittest.main()
