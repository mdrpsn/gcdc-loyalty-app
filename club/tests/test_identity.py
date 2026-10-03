from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.permissions import ADMIN, ORGANIZER
from club.models import Attendance, AuditEvent, OpenPlay, Player, RewardIssuance, Venue
from club.services import openplays_attended


class IdentityLinkingTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.organizer = User.objects.create_user("organizer@example.com", "Securepass123")
        self.organizer.groups.add(Group.objects.get(name=ORGANIZER))
        self.admin = User.objects.create_user("admin@example.com", "Securepass123")
        self.admin.groups.add(Group.objects.get(name=ADMIN))
        self.member = User.objects.create_user("member@example.com", "Securepass123")
        self.member_player = Player.objects.create(user=self.member, display_name="Linked Member")
        self.account = User.objects.create_user("newmember@example.com", "Securepass123")
        # Provisional email deliberately equals the account email: a match must never link by itself.
        self.walk_in = Player.objects.create(
            display_name="Walk In Wanda", phone="09171112222", contact_email="newmember@example.com",
        )
        venue = Venue.objects.create(name="Main Court")
        start = timezone.now() - timedelta(days=3)
        self.session = OpenPlay.objects.create(
            venue=venue, starts_at=start, ends_at=start + timedelta(hours=2),
            status=OpenPlay.Status.COMPLETED, completed_at=start + timedelta(hours=2),
        )
        self.attendance = Attendance.objects.create(
            player=self.walk_in, openplay=self.session, source=Attendance.Source.WALK_IN,
            recorded_by=self.organizer,
        )
        self.search_url = reverse("organizer:identity")
        self.link_url = reverse("organizer:identity_link", args=[self.walk_in.pk])

    def link_post(self, user=None, verified=True, **extra):
        data = {"user_id": (user or self.account).pk}
        if verified:
            data["verified"] = "on"
        return self.client.post(self.link_url, data, **extra)

    def assert_unlinked(self):
        self.walk_in.refresh_from_db()
        self.assertIsNone(self.walk_in.user_id)
        self.assertEqual(self.walk_in.contact_email, "newmember@example.com")
        self.assertFalse(AuditEvent.objects.filter(action="PLAYER_LINKED").exists())

    def test_anonymous_is_sent_to_login(self):
        for response in (self.client.get(self.search_url), self.client.get(self.link_url), self.link_post()):
            self.assertEqual(response.status_code, 302)
            self.assertIn("/login/", response["Location"])
        self.assert_unlinked()

    def test_player_and_organizer_are_forbidden_from_every_identity_action(self):
        for user in (self.member, self.organizer):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.search_url).status_code, 403)
            self.assertEqual(self.client.get(self.link_url).status_code, 403)
            self.assertEqual(self.client.get(self.link_url, {"email": "newmember@example.com"}).status_code, 403)
            self.assertEqual(self.link_post().status_code, 403)
        self.assert_unlinked()

    def test_admin_can_open_both_pages(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(self.search_url), "Link an account to a player")
        self.assertContains(self.client.get(self.link_url), "Verify the person")

    def test_navigation_and_player_record_links_are_admin_only(self):
        home = reverse("organizer:home")
        detail = reverse("organizer:player", args=[self.walk_in.pk])
        linked_detail = reverse("organizer:player", args=[self.member_player.pk])
        self.client.force_login(self.organizer)
        self.assertNotContains(self.client.get(home), self.search_url)
        self.assertNotContains(self.client.get(detail), self.link_url)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(home), self.search_url)
        self.assertContains(self.client.get(detail), self.link_url)
        self.assertNotContains(self.client.get(linked_detail), "Link to account")

    def test_search_lists_only_unlinked_players(self):
        self.client.force_login(self.admin)
        found = self.client.get(self.search_url, {"q": "Wanda"})
        self.assertContains(found, "Walk In Wanda")
        self.assertContains(found, self.link_url)
        self.assertNotContains(self.client.get(self.search_url, {"q": "Linked Member"}), self.member_player.display_name + "</strong>")
        self.assertContains(self.client.get(self.search_url, {"q": "Linked Member"}), "No matching unlinked players")
        self.assertNotContains(self.client.get(self.search_url, {"q": "W"}), "Walk In Wanda")

    def test_lookup_shows_both_records_but_never_links_even_when_emails_match(self):
        self.client.force_login(self.admin)
        page = self.client.get(self.link_url, {"email": "NewMember@Example.com"})
        self.assertContains(page, "Walk In Wanda")
        self.assertContains(page, "newmember@example.com")
        self.assertContains(page, "OpenPlays Attended: <strong>1</strong>")
        self.assertContains(page, "No player record yet")
        self.assertContains(page, "Link account")
        self.assert_unlinked()

    def test_lookup_of_unknown_email_is_a_form_error(self):
        self.client.force_login(self.admin)
        page = self.client.get(self.link_url, {"email": "nobody@example.com"})
        self.assertContains(page, "No account has that email.")
        self.assert_unlinked()

    def test_successful_link_clears_provisional_email_keeps_history_and_audits(self):
        self.client.force_login(self.admin)
        before = openplays_attended(self.walk_in)
        response = self.link_post()
        self.assertRedirects(response, reverse("organizer:player", args=[self.walk_in.pk]))
        self.walk_in.refresh_from_db()
        self.assertEqual(self.walk_in.user, self.account)
        self.assertIsNone(self.walk_in.contact_email)
        self.assertEqual(self.walk_in.display_name, "Walk In Wanda")
        self.assertEqual(openplays_attended(self.walk_in), before)
        self.assertEqual(Attendance.objects.get(pk=self.attendance.pk).player_id, self.walk_in.pk)
        self.assertEqual(Player.objects.filter(user=self.account).count(), 1)
        event = AuditEvent.objects.get(action="PLAYER_LINKED", target_id=self.walk_in.pk)
        self.assertEqual((event.actor, event.target_type, event.details["user_id"]),
                         (self.admin, "player", self.account.pk))
        self.assertFalse(RewardIssuance.objects.filter(player=self.walk_in).exists())

    def test_identity_verification_confirmation_is_required(self):
        self.client.force_login(self.admin)
        response = self.link_post(verified=False)
        self.assertEqual(response.status_code, 400)
        self.assert_unlinked()

    def test_already_linked_player_cannot_be_relinked(self):
        other = get_user_model().objects.create_user("other@example.com", "Securepass123")
        url = reverse("organizer:identity_link", args=[self.member_player.pk])
        self.client.force_login(self.admin)
        page = self.client.get(url)
        self.assertContains(page, "already linked to member@example.com")
        self.assertNotContains(page, "Look up account")
        response = self.client.post(url, {"user_id": other.pk, "verified": "on"})
        self.assertContains(response, "already linked to an account", status_code=409)
        self.member_player.refresh_from_db()
        self.assertEqual(self.member_player.user, self.member)
        self.assertFalse(Player.objects.filter(user=other).exists())
        self.assertFalse(AuditEvent.objects.filter(action="PLAYER_LINKED").exists())

    def test_account_that_already_has_a_player_cannot_be_linked_to_a_walk_in(self):
        self.client.force_login(self.admin)
        page = self.client.get(self.link_url, {"email": "member@example.com"})
        self.assertContains(page, "already has a player record")
        self.assertContains(page, "Linked Member")
        self.assertContains(page, "disabled")
        response = self.link_post(user=self.member)
        self.assertContains(response, "already linked to a player", status_code=409)
        self.assert_unlinked()
        self.assertEqual(Player.objects.filter(user=self.member).count(), 1)

    def test_unknown_account_id_is_a_form_error_not_a_500(self):
        self.client.force_login(self.admin)
        response = self.client.post(self.link_url, {"user_id": 999999, "verified": "on"})
        self.assertContains(response, "Account not found.", status_code=409)
        self.assert_unlinked()

    def test_get_never_links_and_csrf_is_enforced(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.admin)
        self.assertEqual(strict.post(self.link_url, {"user_id": self.account.pk, "verified": "on"}).status_code, 403)
        self.client.force_login(self.admin)
        self.client.get(self.link_url, {"email": "newmember@example.com", "user_id": self.account.pk, "verified": "on"})
        self.assert_unlinked()

    def test_linked_account_then_sees_its_history_on_the_player_dashboard(self):
        self.client.force_login(self.admin)
        self.link_post()
        self.client.force_login(self.account)
        page = self.client.get(reverse("player:home"))
        self.assertContains(page, "Walk In Wanda")
        self.assertEqual(page.context["summary"].openplays_attended, 1)
