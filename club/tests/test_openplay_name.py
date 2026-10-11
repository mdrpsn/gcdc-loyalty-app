from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.permissions import ADMIN, ORGANIZER
from club.forms import OpenPlayForm
from club.models import AuditEvent, OpenPlay, Player, Venue
from club.player_forms import OpenPlayChoiceField
from club.services import cancel_openplay, create_openplay, rename_openplay


class OpenPlayNameTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.organizer = User.objects.create_user("organizer@example.com", "Securepass123")
        self.organizer.groups.add(Group.objects.get(name=ORGANIZER))
        self.admin = User.objects.create_user("admin@example.com", "Securepass123")
        self.admin.groups.add(Group.objects.get(name=ADMIN))
        self.member = User.objects.create_user("member@example.com", "Securepass123")
        Player.objects.create(user=self.member, display_name="Maya Santos", phone="111")
        self.venue = Venue.objects.create(name="Pickle Star")
        self.start = timezone.now() + timedelta(days=2)
        self.end = self.start + timedelta(hours=2)

    def make(self, **extra):
        return create_openplay(venue_id=self.venue.pk, starts_at=self.start, ends_at=self.end,
                               actor=self.organizer, **extra)

    # --- default name: no database id in it

    def test_unnamed_session_reads_openplay_at_venue_without_the_database_id(self):
        openplay = self.make()
        self.assertEqual(openplay.name, "")
        self.assertEqual(openplay.display_name, "OpenPlay at Pickle Star")
        self.assertEqual(str(openplay), "OpenPlay at Pickle Star")
        self.assertNotIn(str(openplay.pk) + " at", str(openplay))

    # --- custom name at scheduling

    def test_a_custom_name_is_saved_trimmed_shown_and_audited(self):
        openplay = self.make(name="  Sunday Morning Dinkers  ")
        self.assertEqual(openplay.name, "Sunday Morning Dinkers")
        self.assertEqual(str(openplay), "Sunday Morning Dinkers")
        created = AuditEvent.objects.get(action="OPENPLAY_CREATED", target_id=openplay.pk)
        self.assertEqual(created.details["name"], "Sunday Morning Dinkers")
        self.client.force_login(self.organizer)
        page = self.client.get(reverse("organizer:session", args=[openplay.pk]))
        self.assertContains(page, "Sunday Morning Dinkers")
        html = page.content.decode()
        title = html[html.index("data-rename-show"):html.index("</h1>")]
        self.assertIn("Sunday Morning Dinkers", title)
        self.assertNotIn("OpenPlay at Pickle Star", title)  # only the editor's placeholder mentions the default

    def test_the_schedule_form_has_an_optional_name_field(self):
        form = OpenPlayForm(data={"venue": self.venue.pk, "starts_at": "2026-12-01T18:00",
                                  "ends_at": "2026-12-01T20:00"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["name"], "")
        too_long = OpenPlayForm(data={"venue": self.venue.pk, "starts_at": "2026-12-01T18:00",
                                      "ends_at": "2026-12-01T20:00", "name": "x" * 81})
        self.assertFalse(too_long.is_valid())
        self.assertIn("name", too_long.errors)

    def test_posting_the_schedule_form_with_a_name_creates_the_named_session(self):
        self.client.force_login(self.organizer)
        response = self.client.post(reverse("organizer:openplay_new"), {
            "venue": self.venue.pk, "name": "Friday Night Rally",
            "starts_at": "2026-12-04T18:00", "ends_at": "2026-12-04T20:00",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(OpenPlay.objects.get().name, "Friday Night Rally")

    def test_an_overlong_name_is_rejected_and_nothing_is_created(self):
        with self.assertRaises(ValidationError):
            self.make(name="x" * 81)
        self.assertFalse(OpenPlay.objects.exists())

    # --- editing the name later

    def test_organizer_and_admin_can_rename_and_it_is_audited(self):
        openplay = self.make(name="Old name")
        for actor, new in ((self.organizer, "Tuesday Social"), (self.admin, "Tuesday Social Night")):
            rename_openplay(openplay_id=openplay.pk, name=new, actor=actor)
            openplay.refresh_from_db()
            self.assertEqual(openplay.name, new)
        events = list(AuditEvent.objects.filter(action="OPENPLAY_RENAMED", target_id=openplay.pk).order_by("pk"))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].details, {"previous_name": "Old name", "name": "Tuesday Social"})
        self.assertEqual(events[0].actor, self.organizer)
        self.assertEqual(events[1].actor, self.admin)

    def test_renaming_to_blank_returns_to_the_default_name(self):
        openplay = self.make(name="Temporary")
        rename_openplay(openplay_id=openplay.pk, name="   ", actor=self.organizer)
        openplay.refresh_from_db()
        self.assertEqual(openplay.name, "")
        self.assertEqual(str(openplay), "OpenPlay at Pickle Star")

    def test_rename_rejects_overlong_names_and_players_cannot_rename(self):
        openplay = self.make(name="Keep me")
        with self.assertRaises(ValidationError):
            rename_openplay(openplay_id=openplay.pk, name="y" * 81, actor=self.organizer)
        with self.assertRaises(PermissionDenied):
            rename_openplay(openplay_id=openplay.pk, name="Hacked", actor=self.member)
        openplay.refresh_from_db()
        self.assertEqual(openplay.name, "Keep me")
        self.assertFalse(AuditEvent.objects.filter(action="OPENPLAY_RENAMED").exists())

    def test_a_cancelled_session_can_still_be_renamed_and_keeps_its_history(self):
        openplay = self.make(name="Typo nmae")
        cancel_openplay(openplay_id=openplay.pk, actor=self.organizer, reason="Rain")
        rename_openplay(openplay_id=openplay.pk, name="Typo name", actor=self.organizer)
        openplay.refresh_from_db()
        self.assertEqual(openplay.name, "Typo name")
        self.assertEqual(openplay.status, OpenPlay.Status.CANCELLED)
        self.assertEqual(openplay.cancellation_reason, "Rain")

    def test_rename_screen_prefills_saves_and_enforces_roles(self):
        openplay = self.make(name="Before")
        url = reverse("organizer:rename", args=[openplay.pk])
        self.assertIn("/login/", self.client.get(url)["Location"])
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {"name": "Hacked"}).status_code, 403)
        self.client.force_login(self.organizer)
        self.assertContains(self.client.get(url), 'value="Before"')
        response = self.client.post(url, {"name": "After"})
        self.assertRedirects(response, reverse("organizer:session", args=[openplay.pk]))
        openplay.refresh_from_db()
        self.assertEqual(openplay.name, "After")
        too_long = self.client.post(url, {"name": "z" * 81})
        self.assertEqual(too_long.status_code, 400)
        openplay.refresh_from_db()
        self.assertEqual(openplay.name, "After")

    def test_session_page_has_an_inline_rename_form_in_the_heading(self):
        openplay = self.make(name="Sunday Dinkers")
        rename_url = reverse("organizer:rename", args=[openplay.pk])
        self.client.force_login(self.organizer)
        page = self.client.get(reverse("organizer:session", args=[openplay.pk]))
        html = page.content.decode()
        # The name itself is the control: a POST form to the audited rename endpoint, with CSRF.
        form_start = html.index(f'action="{rename_url}"')
        form = html[form_start:html.index("</form>", form_start)]
        self.assertIn("csrfmiddlewaretoken", html[html.rindex("<form", 0, form_start):form_start + len(form)])
        self.assertIn('name="name"', form)
        self.assertIn('value="Sunday Dinkers"', form)
        self.assertIn('maxlength="80"', form)
        self.assertIn('aria-label="Save name"', form)
        self.assertIn("data-rename-cancel", form)
        # Visible without scrolling: before the player search and the session actions.
        self.assertLess(form_start, html.index("Find a player"))
        self.assertLess(form_start, html.index("Session actions"))

    def test_inline_form_opens_with_the_full_current_name_even_when_unnamed(self):
        # Opening the editor must never look like the name was erased: an unnamed session shows
        # its default name as real text so a few letters can be changed.
        openplay = self.make()
        self.client.force_login(self.organizer)
        page = self.client.get(reverse("organizer:session", args=[openplay.pk]))
        self.assertContains(page, 'value="OpenPlay at Pickle Star"')

    def test_inline_script_keeps_the_text_and_puts_the_cursor_at_the_end(self):
        openplay = self.make(name="Sunday Dinkers")
        self.client.force_login(self.organizer)
        script = self.client.get(reverse("organizer:session", args=[openplay.pk])).content.decode()
        script = script[script.index("<script>"):script.index("</script>")]
        self.assertNotIn(".select()", script)  # select-all makes the first keystroke replace everything
        self.assertIn("setSelectionRange", script)

    def test_saving_the_default_text_unchanged_keeps_the_session_unnamed(self):
        openplay = self.make()
        rename_openplay(openplay_id=openplay.pk, name="OpenPlay at Pickle Star", actor=self.organizer)
        openplay.refresh_from_db()
        self.assertEqual(openplay.name, "")
        self.assertFalse(AuditEvent.objects.filter(action="OPENPLAY_RENAMED").exists())

    def test_editing_a_few_letters_of_the_default_name_saves_a_custom_name(self):
        openplay = self.make()
        rename_openplay(openplay_id=openplay.pk, name="OpenPlay at Pickle Star (Beginners)", actor=self.organizer)
        openplay.refresh_from_db()
        self.assertEqual(openplay.name, "OpenPlay at Pickle Star (Beginners)")

    def test_posting_from_the_inline_form_saves_and_returns_to_the_same_session(self):
        openplay = self.make(name="Before")
        self.client.force_login(self.organizer)
        page = self.client.post(reverse("organizer:rename", args=[openplay.pk]), {"name": "After inline"}, follow=True)
        self.assertEqual(page.redirect_chain, [(reverse("organizer:session", args=[openplay.pk]), 302)])
        self.assertContains(page, "OpenPlay name saved.")
        self.assertContains(page, "After inline")

    def test_without_javascript_there_is_still_a_link_to_the_rename_screen(self):
        openplay = self.make()
        self.client.force_login(self.organizer)
        page = self.client.get(reverse("organizer:session", args=[openplay.pk]))
        self.assertContains(page, "<noscript>")
        self.assertContains(page, "Rename OpenPlay")

    # --- what players see

    def test_players_see_the_custom_name_or_the_venue_fallback(self):
        named = self.make(name="Saturday Open Court")
        plain = create_openplay(venue_id=self.venue.pk, starts_at=self.start + timedelta(days=1),
                                ends_at=self.end + timedelta(days=1), actor=self.organizer)
        self.client.force_login(self.member)
        page = self.client.get(reverse("player:openplays"))
        self.assertContains(page, "Saturday Open Court")
        self.assertContains(page, "OpenPlay at Pickle Star")
        label = OpenPlayChoiceField(queryset=OpenPlay.objects.all()).label_from_instance
        self.assertTrue(label(named).startswith("Saturday Open Court · "))
        self.assertFalse(label(plain).startswith("OpenPlay"))
        self.assertIn("Pickle Star", label(named))
