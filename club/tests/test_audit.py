import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import mail
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.permissions import ADMIN, ORGANIZER
from club.loyalty import reconcile_player_loyalty, redeem_reward
from club.models import Attendance, AuditEvent, OpenPlay, Player, Venue
from club.services import (
    add_historical_attendance, cancel_openplay, check_in_walk_in, complete_openplay, create_openplay,
    create_venue, create_walk_in_player, link_player_account, restore_attendance, update_own_profile,
    void_attendance,
)
from club.templatetags.club_audit import MAX_DETAILS_LENGTH, _SECRET_KEY


class AuditReviewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.organizer = User.objects.create_user("organizer@example.com", "Securepass123")
        self.organizer.groups.add(Group.objects.get(name=ORGANIZER))
        self.admin = User.objects.create_user("admin@example.com", "Securepass123")
        self.admin.groups.add(Group.objects.get(name=ADMIN))
        self.member = User.objects.create_user("member@example.com", "Securepass123")
        self.url = reverse("organizer:audit")

    def make_events(self, count, **fields):
        return AuditEvent.objects.bulk_create([
            AuditEvent(action=fields.get("action", f"TEST_{i:03d}"),
                       target_type=fields.get("target_type", "player"), target_id=fields.get("target_id", i + 1),
                       actor=fields.get("actor", self.admin), details=fields.get("details", {}))
            for i in range(count)
        ])

    # --- authorization and read-only ---

    def test_anonymous_is_sent_to_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_player_and_organizer_are_forbidden(self):
        for user in (self.member, self.organizer):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url).status_code, 403)
            self.assertEqual(self.client.get(self.url, {"action": "X"}).status_code, 403)

    def test_admin_can_view_and_nothing_can_mutate(self):
        self.make_events(1)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(self.url), "Audit log")
        before = AuditEvent.objects.count()
        for method in ("post", "put", "patch", "delete"):
            self.assertEqual(getattr(self.client, method)(self.url).status_code, 405, method)
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_navigation_link_is_admin_only_and_player_record_links_to_its_events(self):
        player = Player.objects.create(display_name="Walk In", phone="0917")
        detail = reverse("organizer:player", args=[player.pk])
        self.client.force_login(self.organizer)
        self.assertNotContains(self.client.get(reverse("organizer:home")), self.url)
        self.assertNotContains(self.client.get(detail), "Audit events")
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("organizer:home")), f'href="{self.url}"')
        self.assertContains(self.client.get(detail), f"{self.url}?target_type=player&amp;target_id={player.pk}")

    # --- ordering, pagination, display ---

    def test_newest_first_and_paginated_at_50(self):
        self.make_events(120)
        self.client.force_login(self.admin)
        first = self.client.get(self.url)
        self.assertEqual(len(first.context["page"]), 50)
        self.assertEqual(first.context["page"].paginator.count, 120)
        self.assertEqual([event.action for event in first.context["page"]][:2], ["TEST_119", "TEST_118"])
        third = self.client.get(self.url, {"page": 3})
        self.assertEqual(len(third.context["page"]), 20)
        self.assertEqual(third.context["page"][-1].action, "TEST_000")
        self.assertContains(first, "Page 1 of 3")

    def test_pagination_links_keep_filters(self):
        self.make_events(60, target_type="player")
        self.client.force_login(self.admin)
        page = self.client.get(self.url, {"target_type": "player"})
        self.assertContains(page, "target_type=player&amp;page=2")

    def test_system_event_and_actor_event_display(self):
        AuditEvent.objects.create(action="PASSWORD_RESET_COMPLETED", target_type="user", target_id=7)
        AuditEvent.objects.create(action="VENUE_CREATED", target_type="venue", target_id=1, actor=self.admin)
        self.client.force_login(self.admin)
        page = self.client.get(self.url)
        self.assertContains(page, "system")
        self.assertContains(page, "admin@example.com")
        self.assertContains(page, "venue #1")

    @override_settings(TIME_ZONE="Asia/Manila")
    def test_time_is_shown_in_club_timezone(self):
        AuditEvent.objects.create(action="X", target_type="venue", target_id=1, actor=self.admin)
        self.client.force_login(self.admin)
        local = timezone.localtime(AuditEvent.objects.get().occurred_at)
        expected = f"{int(local.strftime('%I'))}:{local.strftime('%M:%S %p')} {local.strftime('%Z')}"
        self.assertEqual(local.utcoffset(), timedelta(hours=8))
        self.assertContains(self.client.get(self.url), expected)

    # --- filters ---

    def test_each_filter_narrows_results(self):
        other = get_user_model().objects.create_user("other@example.com", "Securepass123")
        AuditEvent.objects.create(action="REWARD_ISSUED", target_type="rewardissuance", target_id=5, actor=self.admin)
        AuditEvent.objects.create(action="PLAYER_LINKED", target_type="player", target_id=9, actor=other)
        AuditEvent.objects.create(action="PLAYER_CREATED", target_type="player", target_id=10)
        self.client.force_login(self.admin)

        def actions(**params):
            return sorted(event.action for event in self.client.get(self.url, params).context["page"])

        self.assertEqual(actions(action="reward_issued"), ["REWARD_ISSUED"])
        self.assertEqual(actions(target_type="PLAYER"), ["PLAYER_CREATED", "PLAYER_LINKED"])
        self.assertEqual(actions(target_id="9"), ["PLAYER_LINKED"])
        self.assertEqual(actions(actor="OTHER@example.com"), ["PLAYER_LINKED"])
        self.assertEqual(actions(target_type="player", target_id="10"), ["PLAYER_CREATED"])
        self.assertEqual(actions(action="NO_SUCH_ACTION"), [])
        self.assertContains(self.client.get(self.url, {"action": "NO_SUCH_ACTION"}), "No matching audit events")

    def test_invalid_filters_are_ignored_not_applied_and_never_error(self):
        self.make_events(3)
        self.client.force_login(self.admin)
        for params in ({"target_id": "abc"}, {"target_id": "0"}, {"target_id": str(10**30)},
                       {"actor": "not-an-email"}, {"action": "x" * 500}, {"page": "zzz"}):
            response = self.client.get(self.url, params)
            self.assertEqual(response.status_code, 200, params)
            self.assertEqual(response.context["page"].paginator.count, 3, params)
        self.assertContains(self.client.get(self.url, {"target_id": "abc"}), "Enter a whole number")

    def test_valid_filter_still_applies_when_another_is_invalid(self):
        AuditEvent.objects.create(action="ONLY_ME", target_type="venue", target_id=1)
        AuditEvent.objects.create(action="OTHER", target_type="venue", target_id=2)
        self.client.force_login(self.admin)
        response = self.client.get(self.url, {"action": "only_me", "target_id": "abc"})
        self.assertEqual([event.action for event in response.context["page"]], ["ONLY_ME"])

    # --- query budget ---

    def test_query_count_is_bounded_and_does_not_grow_with_events(self):
        actors = [get_user_model().objects.create_user(f"actor{i}@example.com", "Securepass123") for i in range(5)]
        for index, actor in enumerate(actors):
            self.make_events(10, actor=actor, target_id=index + 1)
        self.client.force_login(self.admin)
        with CaptureQueriesContext(connection) as small:
            self.client.get(self.url)
        for index, actor in enumerate(actors):
            self.make_events(10, actor=actor, target_id=index + 100)
        with CaptureQueriesContext(connection) as large:
            self.client.get(self.url)
        self.assertLessEqual(len(small), 12)
        self.assertEqual(len(small), len(large))

    # --- safety: escaping, redaction, length cap ---

    def test_details_are_escaped(self):
        AuditEvent.objects.create(action="X", target_type="venue", target_id=1, actor=self.admin,
                                  details={"reason": "<script>alert(1)</script>"})
        self.client.force_login(self.admin)
        page = self.client.get(self.url)
        self.assertNotContains(page, "<script>alert(1)</script>")
        self.assertContains(page, "&lt;script&gt;alert(1)&lt;/script&gt;")

    def test_secret_looking_keys_are_hidden_even_if_nested_and_length_is_capped(self):
        AuditEvent.objects.create(
            action="X", target_type="user", target_id=1, actor=self.admin,
            details={"password": "hunter2-value", "reset_token": "tok-abc123", "note": "n" * 1000,
                     "changes": {"api_key": "key-xyz789", "email": {"before": "a@x.io", "after": "b@x.io"}},
                     "items": [{"session_id": "sess-777"}]},
        )
        self.client.force_login(self.admin)
        page = self.client.get(self.url)
        for secret in ("hunter2-value", "tok-abc123", "key-xyz789", "sess-777"):
            self.assertNotContains(page, secret)
        self.assertContains(page, "[hidden]")
        text = re.search(r'<code class="audit-details">(.*?)</code>', page.content.decode(), re.S).group(1)
        self.assertLessEqual(len(text.replace("&quot;", '"')), MAX_DETAILS_LENGTH + 1)
        self.assertIn("…", text)
        self.assertNotIn("n" * 200, text)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AuditDetailsContainNoSecretsTests(TestCase):
    """Run the real mutations, then prove no stored details key or value carries a credential."""

    def test_real_write_paths_store_no_credentials(self):
        User = get_user_model()
        organizer = User.objects.create_user("organizer@example.com", "organizer-pass-123")
        organizer.groups.add(Group.objects.get(name=ORGANIZER))
        admin = User.objects.create_user("admin@example.com", "admin-pass-123")
        admin.groups.add(Group.objects.get(name=ADMIN))
        member = User.objects.create_user("member@example.com", "member-pass-123")
        venue = create_venue(name="Main Court", actor=organizer)
        now = timezone.now()

        def session(days_ago):
            start = now - timedelta(days=days_ago)
            return create_openplay(venue_id=venue.pk, starts_at=start, ends_at=start + timedelta(hours=2), actor=organizer)

        first = session(3)
        walk_in, attendance = check_in_walk_in(
            openplay_id=first.pk, display_name="Walk In", phone="0917", actor=organizer, now=first.starts_at)
        complete_openplay(openplay_id=first.pk, actor=organizer, now=first.ends_at)
        void_attendance(attendance_id=attendance.pk, actor=admin, reason="Mistaken entry")
        restore_attendance(attendance_id=attendance.pk, actor=admin, reason="Verified")
        other = create_walk_in_player(display_name="Other", phone="0918", actor=organizer)
        add_historical_attendance(openplay_id=first.pk, player_id=other.pk, actor=admin, reason="Paper roster")
        link_player_account(player_id=walk_in.pk, user_id=member.pk, actor=admin)
        update_own_profile(player_id=walk_in.pk, display_name="Walker", phone="0999", actor=member)
        cancel_openplay(openplay_id=session(2).pk, actor=organizer, reason="Rain")
        for days_ago in range(4, 13):
            old = session(days_ago)
            Attendance.objects.create(player=walk_in, openplay=old, source=Attendance.Source.HISTORICAL, recorded_by=admin)
            OpenPlay.objects.filter(pk=old.pk).update(
                status=OpenPlay.Status.COMPLETED, completed_at=old.ends_at)
        reconcile_player_loyalty(walk_in, actor=admin)
        issuance = walk_in.reward_issuances.get()
        redeem_reward(issuance_id=issuance.pk, player_id=walk_in.pk, actor=organizer)
        # Snapshot every hash that exists before the reset changes one; a leak of an old hash must be caught too.
        seen_hashes = set(User.objects.values_list("password", flat=True))
        self.client.post(reverse("password_reset"), {"email": member.email})
        path = re.search(r"http://testserver(/password-reset/[^\s]+)", mail.outbox[0].body).group(1)
        redirect = self.client.get(path)
        self.client.post(redirect["Location"], {
            "new_password1": "brand-new-pass-456", "new_password2": "brand-new-pass-456"})

        actions = set(AuditEvent.objects.values_list("action", flat=True))
        for required in ("VENUE_CREATED", "OPENPLAY_COMPLETED", "ATTENDANCE_VOIDED", "PLAYER_LINKED",
                         "OPENPLAY_CANCELLED", "REWARD_ISSUED", "REWARD_REDEEMED", "PASSWORD_RESET_COMPLETED"):
            self.assertIn(required, actions)

        def keys(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    yield str(key)
                    yield from keys(item)
            elif isinstance(value, list):
                for item in value:
                    yield from keys(item)

        stored = [event.details for event in AuditEvent.objects.all()]
        for details in stored:
            for key in keys(details):
                self.assertIsNone(_SECRET_KEY.search(key), key)
        blob = str(stored)
        token = path.rstrip("/").split("/")[-1]
        for secret in ("organizer-pass-123", "admin-pass-123", "member-pass-123", "brand-new-pass-456", token):
            self.assertNotIn(secret, blob)
        self.assertNotIn("pbkdf2", blob)
        # Hasher-independent: no user's stored password hash may appear in any audit details.
        seen_hashes |= set(User.objects.values_list("password", flat=True))
        self.assertGreaterEqual(len(seen_hashes), 4)  # three originals plus the member's new hash
        for stored_hash in seen_hashes:
            self.assertTrue(stored_hash)
            self.assertNotIn(stored_hash, blob)
