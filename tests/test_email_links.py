"""
Unit tests for Markdown links in the HTML email body.

The model writes official links as [url](url). Before this fix the email showed
that syntax verbatim, so every item ended in two copies of the same long URL.

    python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lambda_function as lf  # noqa: E402

URL = 'https://aws.amazon.com/about-aws/whats-new/2026/10/amazon-eks-distro-kubernetes-version-1-37'


class MarkdownLinkTest(unittest.TestCase):

    def test_bare_url_link_uses_label(self):
        out = lf.markdown_to_html(f'**官方連結**：[{URL}]({URL})', link_label='官方公告 ↗')
        self.assertIn(f'<a href="{URL}"', out)
        self.assertIn('>官方公告 ↗</a>', out)
        self.assertNotIn('](', out)
        self.assertEqual(out.count(URL), 1)

    def test_named_link_keeps_its_text(self):
        out = lf.markdown_to_html(f'- see [the announcement]({URL})')
        self.assertIn(f'<a href="{URL}"', out)
        self.assertIn('>the announcement</a>', out)

    def test_link_inside_bold_line_and_blog_arrow(self):
        md = f'**Some blog post**：summary → [{URL}]({URL})'
        out = lf.markdown_to_html(md)
        self.assertIn('<strong>Some blog post</strong>', out)
        self.assertIn('>Read more ↗</a>', out)

    def test_multiple_links_on_one_line(self):
        out = lf.markdown_to_html(f'[a]({URL}) and [b](https://example.com/x)')
        self.assertIn('>a</a>', out)
        self.assertIn('href="https://example.com/x"', out)

    def test_quote_in_url_cannot_break_out_of_href(self):
        out = lf.markdown_to_html('[x](https://example.com/a"onmouseover="y)')
        self.assertNotIn('"onmouseover="', out)

    def test_non_http_brackets_are_left_alone(self):
        out = lf.markdown_to_html('[TODO](not-a-url) stays as text')
        self.assertIn('[TODO](not-a-url)', out)
        self.assertNotIn('<a ', out)

    def test_both_languages_define_link_label(self):
        for lang, strings in lf._EMAIL_STRINGS.items():
            self.assertIn('link_label', strings, lang)


if __name__ == '__main__':
    unittest.main()
