"""Tests for email_template.py's shared shell/badge primitives, including issue #197's light-mode
`@media (prefers-color-scheme: light)` overrides."""

import re
import unittest

from fpl_intel.notifications import email_template


class BadgeHtmlTests(unittest.TestCase):
    def test_known_variant_renders_its_literal_dark_colors_and_a_matching_class(self):
        html = email_template.badge_html("ROLL", "roll")

        self.assertIn(f'background:{email_template.BADGE_ROLL_BG}', html)
        self.assertIn(f'color:{email_template.BADGE_ROLL_FG}', html)
        self.assertIn('class="badge-roll"', html)
        self.assertIn("ROLL", html)

    def test_every_variant_is_renderable(self):
        for variant in ("roll", "hit", "info", "amber", "slate", "docs"):
            with self.subTest(variant=variant):
                html = email_template.badge_html("LABEL", variant)
                self.assertIn(f'class="badge-{variant}"', html)

    def test_an_unknown_variant_raises(self):
        with self.assertRaises(KeyError):
            email_template.badge_html("LABEL", "not-a-real-variant")

    def test_label_is_escaped(self):
        html = email_template.badge_html("<script>", "info")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


class ShellLightModeTests(unittest.TestCase):
    """Issue #197: these emails used to declare `content="dark"` only and ship no light palette
    at all -- `shell()` now declares `"light dark"` and carries a `<style>` block with every
    token's light-mode override."""

    def setUp(self):
        self.html = email_template.shell("Test Title", "<tr><td>body</td></tr>")

    def test_color_scheme_meta_tags_declare_both_themes(self):
        self.assertIn('<meta name="color-scheme" content="light dark">', self.html)
        self.assertIn('<meta name="supported-color-schemes" content="light dark">', self.html)
        self.assertNotIn('content="dark">', self.html)

    def test_carries_a_prefers_color_scheme_light_style_block(self):
        self.assertIn("<style>", self.html)
        self.assertIn("@media (prefers-color-scheme:light)", self.html)

    def test_style_block_precedes_the_body_so_it_can_apply_to_it(self):
        style_index = self.html.index("<style>")
        body_index = self.html.index("<body")
        self.assertLess(style_index, body_index)

    def test_every_light_override_class_is_declared_with_important(self):
        style_block = re.search(r"<style>(.*?)</style>", self.html, re.DOTALL).group(1)
        # A missing !important would silently lose to the inline dark default in every real
        # email client, so every single declared property (not just the rule as a whole) needs
        # its own !important -- checked per rule, not just "the block contains !important
        # somewhere", so one property slipping through unguarded wouldn't be masked by another.
        for class_name, css in email_template._LIGHT_MODE_RULES.items():
            with self.subTest(class_name=class_name):
                self.assertIn(f".{class_name}{{", style_block)
                for declaration in css.split(";"):
                    self.assertIn("!important", declaration)

    def test_every_light_value_appears_somewhere_in_the_style_block(self):
        style_block = re.search(r"<style>(.*?)</style>", self.html, re.DOTALL).group(1)
        light_constants = [
            email_template.EMAIL_BG_LIGHT, email_template.CARD_BG_LIGHT,
            email_template.CARD_BORDER_LIGHT, email_template.TEXT_PRIMARY_LIGHT,
            email_template.TEXT_MUTED_LIGHT, email_template.SURFACE_INSET_BG_LIGHT,
            email_template.AMBER_NOTE_BORDER_LIGHT,
            email_template.BADGE_ROLL_BG_LIGHT, email_template.BADGE_ROLL_FG_LIGHT,
            email_template.BADGE_HIT_BG_LIGHT, email_template.BADGE_HIT_FG_LIGHT,
            email_template.BADGE_INFO_BG_LIGHT, email_template.BADGE_INFO_FG_LIGHT,
            email_template.BADGE_AMBER_BG_LIGHT, email_template.BADGE_AMBER_FG_LIGHT,
            email_template.BADGE_SLATE_BG_LIGHT, email_template.BADGE_SLATE_FG_LIGHT,
            email_template.BADGE_DOCS_BG_LIGHT, email_template.BADGE_DOCS_FG_LIGHT,
        ]
        for value in light_constants:
            with self.subTest(value=value):
                self.assertIn(value, style_block)

    def test_body_and_outer_table_carry_the_email_bg_class(self):
        self.assertIn('class="email-bg"', self.html)

    def test_title_is_escaped(self):
        html = email_template.shell("<script>bad</script>", "")
        self.assertNotIn("<script>bad</script>", html)

    def test_inner_html_is_preserved_verbatim(self):
        self.assertIn("<tr><td>body</td></tr>", self.html)

    def test_dark_inline_defaults_are_unchanged_from_before_the_light_palette(self):
        """The dark styling itself -- what every client without prefers-color-scheme support
        (Outlook desktop) permanently sees -- must be byte-identical to before issue #197."""
        self.assertIn(f'background:{email_template.EMAIL_BG}', self.html)


if __name__ == "__main__":
    unittest.main()
