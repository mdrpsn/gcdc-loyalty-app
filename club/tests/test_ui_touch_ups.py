import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from accounts.permissions import ORGANIZER
from club.models import Venue

CSS = Path(__file__).resolve().parents[1] / "static" / "club" / "css" / "redesign.css"
MARKER = "/* Accessibility touch-ups"


def touch_up_block():
    text = CSS.read_text(encoding="utf-8")
    assert MARKER in text, "the accessibility touch-up block is missing from redesign.css"
    block = text[text.index(MARKER):]
    return re.sub(r"/\*.*?\*/", "", block, flags=re.S)  # comments would otherwise glue onto the first selector


def rule(block, selector):
    """The declarations of the first rule whose selector list contains `selector`."""
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", block):
        selectors = [s.strip() for s in match.group(1).split(",")]
        if selector in selectors:
            return match.group(2)
    raise AssertionError(f"no rule for {selector!r} in the touch-up block")


class FormHelpTextTests(TestCase):
    def test_the_help_text_exists_under_the_id_the_field_points_to(self):
        # Django sets aria-describedby="id_name_helptext" on a field with help text; the element must exist
        # or screen readers get a dangling reference.
        organizer = get_user_model().objects.create_user("organizer@example.com", "Securepass123")
        organizer.groups.add(Group.objects.get(name=ORGANIZER))
        Venue.objects.create(name="Main Court")
        self.client.force_login(organizer)
        html = self.client.get(reverse("organizer:openplay_new")).content.decode()
        described_by = re.search(r'<input[^>]*name="name"[^>]*aria-describedby="([^"]+)"', html).group(1).split()
        self.assertIn("id_name_helptext", described_by)
        for target in described_by:
            if target.endswith("_helptext"):
                self.assertRegex(html, rf'id="{target}"')


class TouchTargetStylesTests(SimpleTestCase):
    """Guards the rules found by the UI check: tap targets of at least 44 px and readable text sizes."""

    def test_back_links_and_inline_actions_are_44px_tall(self):
        block = touch_up_block()
        for selector in (".back-link", ".section-row > a", ".appbar-brand"):
            with self.subTest(selector=selector):
                self.assertRegex(rule(block, selector), r"min-height:\s*44px")

    def test_navigation_text_is_at_least_14px_and_header_links_are_44px_wide(self):
        block = touch_up_block()
        self.assertRegex(rule(block, ".nav-links a"), r"font-size:\s*\.875rem")
        self.assertRegex(rule(block, ".nav-links a"), r"min-width:\s*44px")

    def test_tab_bar_and_caption_text_is_at_least_12px(self):
        block = touch_up_block()
        self.assertRegex(rule(block, ".tab-label"), r"font-size:\s*\.75rem")
        self.assertRegex(rule(block, ".eyebrow"), r"font-size:\s*\.75rem")
        self.assertRegex(rule(block, ".badge-label"), r"font-size:\s*\.7rem")
        self.assertRegex(rule(block, ".pill"), r"font-size:\s*\.75rem")
        self.assertRegex(rule(block, ".score-strip span"), r"font-size:\s*\.75rem")
