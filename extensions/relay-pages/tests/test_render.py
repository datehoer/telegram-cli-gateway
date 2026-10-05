from __future__ import annotations

import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from relay_pages.render import reading_minutes, render_markdown


def parse_elements(markup: str) -> list[tuple[str, list[tuple[str, str | None]]]]:
    """Real elements and attributes (escaped text such as &lt;a href=…&gt; is not one)."""
    found: list[tuple[str, list[tuple[str, str | None]]]] = []

    class Collector(HTMLParser):
        def handle_starttag(self, tag, attrs):
            found.append((tag, attrs))

    Collector().feed(markup)
    return found


def render(source: str, **kwargs):
    return render_markdown(source, page_id="abcdefgh12345678", **kwargs)


class TitleAndTocTests(unittest.TestCase):
    def test_first_h1_becomes_the_title_and_leaves_the_body(self) -> None:
        page = render("# 部署日志汇总\n\n正文第一段。\n\n## 结果\n")
        self.assertEqual(page.title, "部署日志汇总")
        self.assertNotIn("部署日志汇总", page.body_html)
        self.assertIn('<h2 id="s1">结果</h2>', page.body_html)

    def test_explicit_title_replaces_a_leading_h1(self) -> None:
        page = render("# Old\n\nbody", title="New")
        self.assertEqual(page.title, "New")
        self.assertNotIn("Old", page.body_html)

    def test_without_h1_the_first_line_is_the_title(self) -> None:
        page = render("这是一段很长的开头文字" * 10 + "\n\nmore")
        self.assertEqual(len(page.title), 40)
        self.assertTrue(page.title.endswith("…"))

    def test_toc_lists_h2_and_h3_only_and_demotes_extra_h1(self) -> None:
        page = render("# T\n\n# Second top\n\n### Sub\n\n#### Deep\n\n##### Deeper\n")
        self.assertEqual(
            page.toc,
            [{"level": 2, "text": "Second top", "id": "s1"}, {"level": 3, "text": "Sub", "id": "s2"}],
        )
        self.assertIn('<h4 id="s3">Deep</h4>', page.body_html)
        self.assertIn('<h4 id="s4">Deeper</h4>', page.body_html)


class ComponentTests(unittest.TestCase):
    def test_alerts_map_to_callouts_with_labels(self) -> None:
        page = render("> [!WARNING]\n> 缓存未预热\n\n> [!CAUTION]\n> 删库\n\n> [!TIP]\n> ok\n\n> [!NOTE]\n> fyi\n")
        body = page.body_html
        self.assertIn('<div class="rp-callout warn"><div class="rp-callout-label">', body)
        self.assertIn('rp-icon-warn" aria-hidden="true"></span>Warning</div>', body)
        self.assertIn('class="rp-callout danger"', body)
        self.assertIn("Caution</div>", body)
        self.assertIn("Tip</div>", body)
        self.assertIn('class="rp-callout ok"', body)
        self.assertIn('class="rp-callout"', body)
        self.assertNotIn("markdown-alert", body)
        self.assertEqual(body.count("Warning"), 1)  # markdown-it's own title paragraph is dropped

    def test_tables_are_wrapped_and_numeric_columns_marked(self) -> None:
        source = "| 任务 | 状态 | 耗时 | 备注 |\n|---|:-:|---|---|\n| build | 完成 | 1m 42s | 12% |\n| ops | 失败 | 0m 03s | n/a |\n"
        body = render(source).body_html
        self.assertIn('<div class="rp-table-wrap"><table class="rp-table">', body)
        self.assertIn('<th class="num">耗时</th>', body)
        self.assertIn('<td class="num">1m 42s</td>', body)
        self.assertIn('<td class="center">完成</td>', body)
        self.assertIn("<td>12%</td>", body)  # one non-numeric cell keeps the column left-aligned
        self.assertNotIn("style=", body)

    def test_code_blocks_get_a_language_bar_and_four_colour_highlighting(self) -> None:
        body = render('```python\n# note\nx = "s" + 1\nif x:\n    pass\n```\n').body_html
        self.assertIn('<div class="rp-code" data-lines="4">', body)
        self.assertIn('<span class="rp-code-lang">python</span>', body)
        self.assertIn('<span class="tok-c"># note</span>', body)
        self.assertIn('<span class="tok-s">"s"</span>', body)
        self.assertIn('<span class="tok-n">1</span>', body)
        self.assertIn('<span class="tok-k">if</span>', body)

    def test_unknown_language_and_plain_blocks_are_escaped_text(self) -> None:
        body = render("```nosuchlang\n<b>x</b>\n```\n\n    indented <i>\n").body_html
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", body)
        self.assertIn('<span class="rp-code-lang">text</span>', body)

    def test_mark_task_lists_footnotes_and_line_breaks(self) -> None:
        body = render("==重点== and a == b == c\n\n- [x] done\n- [ ] todo\n\nnote[^1]\nnext line\n\n[^1]: text\n").body_html
        self.assertIn("<mark>重点</mark>", body)
        self.assertIn("a == b == c", body)
        self.assertIn('class="task-list-item-checkbox"', body)
        self.assertIn('class="footnotes"', body)
        self.assertIn("<br>", body)


class SafetyTests(unittest.TestCase):
    def test_raw_html_and_script_urls_never_survive(self) -> None:
        source = (
            "<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n"
            "[a](javascript:alert(1)) [b](data:text/html,<script>alert(1)</script>) "
            "[c](vbscript:x) <a href=\"javascript:alert(1)\">d</a> <JaVaScRiPt:alert(1)>\n\n"
            "![e](javascript:alert(1)) [ok](https://example.com)\n\n"
            "<details open ontoggle=alert(1)>x</details>\n"
        )
        elements = parse_elements(render(source).body_html)
        tags = {tag for tag, _attrs in elements}
        self.assertFalse(tags & {"script", "iframe", "object", "embed", "style", "details"})
        for tag, attrs in elements:
            for name, value in attrs:
                self.assertFalse(name.startswith("on"), (tag, name))
                if name in {"href", "src"}:
                    self.assertRegex(value or "", r"^(https?:|mailto:|#|/p/)", (tag, name, value))
        self.assertIn(("a", [("href", "https://example.com"), ("target", "_blank"), ("rel", "noopener noreferrer")]), elements)

    def test_local_images_are_copied_and_other_local_paths_become_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "chart.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
            secret = Path(temporary) / "secret.env"
            secret.write_text("TOKEN=1")
            page = render(
                f"![chart]({image}) ![again](chart.png) ![s]({secret}) ![gone](/nope/x.png) ![r](https://e.com/a.png)",
                base_dir=Path(temporary),
            )
        self.assertEqual([name for _path, name in page.images], ["1.png"])
        self.assertEqual(page.body_html.count('src="/p/abcdefgh12345678/files/1.png"'), 2)
        self.assertIn("[image: s]", page.body_html)
        self.assertIn("[image: gone]", page.body_html)
        self.assertIn('src="https://e.com/a.png"', page.body_html)
        self.assertNotIn("secret.env", page.body_html)


class ReadingTimeTests(unittest.TestCase):
    def test_four_hundred_characters_a_minute(self) -> None:
        self.assertEqual(reading_minutes("字" * 400), 1)
        self.assertEqual(reading_minutes("字" * 401), 2)
        self.assertEqual(reading_minutes("word " * 250), 1)


if __name__ == "__main__":
    unittest.main()
