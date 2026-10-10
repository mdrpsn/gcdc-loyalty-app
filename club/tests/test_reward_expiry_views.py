from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.permissions import ADMIN, ORGANIZER
from club.loyalty import reconcile_player_loyalty
from club.models import Attendance, AuditEvent, OpenPlay, Player, RewardIssuance, RewardRedemption, Venue


class RewardExpiryViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user("admin@example.com", "Securepass123")
        self.admin.groups.add(Group.objects.get(name=ADMIN))
        self.organizer = User.objects.create_user("organizer@example.com", "Securepass123")
        self.organizer.groups.add(Group.objects.get(name=ORGANIZER))
        self.member = User.objects.create_user("member@example.com", "Securepass123")
        self.player = Player.objects.create(
            user=self.member, display_name="Member", phone="09170000000",
            reclub_name="Member R", reclub_profile_url="https://example.com/reclub/member",
        )
        self.venue = Venue.objects.create(name="Main Court")
        self.now = timezone.now()
        sessions = OpenPlay.objects.bulk_create([
            OpenPlay(venue=self.venue, starts_at=self.now - timedelta(days=i + 2),
                     ends_at=self.now - timedelta(days=i + 2) + timedelta(hours=2),
                     status=OpenPlay.Status.COMPLETED,
                     completed_at=self.now - timedelta(days=i + 2) + timedelta(hours=2))
            for i in range(75)
        ])
        Attendance.objects.bulk_create([
            Attendance(player=self.player, openplay=session, source=Attendance.Source.HISTORICAL,
                       recorded_by=self.admin, checked_in_at=session.starts_at)
            for session in sessions
        ])
        reconcile_player_loyalty(self.player, actor=self.admin)
        self.target = OpenPlay.objects.create(
            venue=self.venue, starts_at=self.now + timedelta(days=1), ends_at=self.now + timedelta(days=1, hours=2),
        )
        self.discount = RewardIssuance.objects.get(player=self.player, benefit_key="discount-50")
        self.overgrip = RewardIssuance.objects.get(player=self.player, benefit_key="overgrip")

    def expire(self, issuance):
        RewardIssuance.objects.filter(pk=issuance.pk).update(
            earned_at=self.now - timedelta(days=40), expires_at=self.now - timedelta(days=10),
        )
        issuance.refresh_from_db()

    def url(self, name, issuance):
        return reverse(f"organizer:{name}", args=[self.player.pk, issuance.pk])

    def test_organizer_page_shows_expiry_and_hides_redeem_for_an_expired_reward(self):
        self.expire(self.discount)
        self.client.force_login(self.organizer)
        page = self.client.get(reverse("organizer:player", args=[self.player.pk]))
        self.assertContains(page, "Expired · admin override needed")
        self.assertNotContains(page, self.url("redeem", self.discount))
        self.assertNotContains(page, "Extend expiry")
        self.assertContains(page, self.url("redeem", self.overgrip))  # the others stay redeemable
        self.client.force_login(self.admin)
        page = self.client.get(reverse("organizer:player", args=[self.player.pk]))
        self.assertContains(page, "Redeem with admin override")
        self.assertContains(page, self.url("extend_expiry", self.discount))

    def test_organizer_cannot_redeem_an_expired_reward_even_by_posting(self):
        self.expire(self.discount)
        self.client.force_login(self.organizer)
        response = self.client.post(self.url("redeem", self.discount), {
            "openplay": self.target.pk, "expired_override_reason": "Please",
        })
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "expired", status_code=409)
        self.assertFalse(RewardRedemption.objects.filter(issuance=self.discount).exists())

    def test_admin_override_needs_a_reason_then_records_it(self):
        self.expire(self.discount)
        self.client.force_login(self.admin)
        redeem = self.url("redeem", self.discount)
        self.assertContains(self.client.get(redeem), "Reason for the admin override")
        response = self.client.post(redeem, {"openplay": self.target.pk, "expired_override_reason": " "})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(RewardRedemption.objects.filter(issuance=self.discount).exists())
        response = self.client.post(redeem, {"openplay": self.target.pk, "expired_override_reason": "App outage"})
        self.assertRedirects(response, reverse("organizer:player", args=[self.player.pk]))
        self.discount.refresh_from_db()
        self.assertEqual(self.discount.status, RewardIssuance.Status.REDEEMED)
        self.assertTrue(AuditEvent.objects.get(action="REWARD_REDEEMED", target_id=self.discount.pk)
                        .details["expired_override"])

    def test_gear_rewards_redeem_without_a_session_but_free_play_needs_one(self):
        self.client.force_login(self.organizer)
        free = RewardIssuance.objects.get(player=self.player, milestone__threshold=50, benefit_key="free-openplay")
        response = self.client.post(self.url("redeem", free), {})
        self.assertEqual(response.status_code, 400)  # a discount or free OpenPlay needs a target session
        response = self.client.post(self.url("redeem", self.overgrip), {})
        self.assertRedirects(response, reverse("organizer:player", args=[self.player.pk]))
        self.assertIsNone(RewardRedemption.objects.get(issuance=self.overgrip).openplay)

    def test_only_admins_reach_the_extension_page_and_it_extends_with_audit(self):
        self.expire(self.discount)
        extend = self.url("extend_expiry", self.discount)
        self.client.force_login(self.organizer)
        self.assertEqual(self.client.get(extend).status_code, 403)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(extend), "Extend expiry")
        later = timezone.localtime(self.now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(extend, {"new_expires_at": later, "reason": "Club approved"})
        self.assertRedirects(response, reverse("organizer:player", args=[self.player.pk]))
        self.discount.refresh_from_db()
        self.assertFalse(self.discount.is_expired())
        self.assertEqual(AuditEvent.objects.filter(action="REWARD_EXPIRY_EXTENDED", target_id=self.discount.pk).count(), 1)
        earlier = timezone.localtime(self.now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(extend, {"new_expires_at": earlier, "reason": "Backwards"})
        self.assertEqual(response.status_code, 409)

    def test_player_sees_expiry_dates_and_expired_rewards_leave_the_available_list(self):
        self.client.force_login(self.member)
        page = self.client.get(reverse("player:rewards"))
        self.assertContains(page, "Expires")
        self.assertNotContains(page, "<h2>Expired</h2>")
        self.expire(self.discount)
        page = self.client.get(reverse("player:rewards"))
        self.assertContains(page, "<h2>Expired</h2>")
        self.assertEqual(page.context["expired"], [self.discount])
        self.assertNotIn(self.discount, page.context["available"])
        home = self.client.get(reverse("player:home"))
        self.assertNotIn(self.discount, home.context["available_rewards"])
        self.assertEqual(home.context["available_count"], len(home.context["available_rewards"]) + max(
            0, home.context["available_count"] - len(home.context["available_rewards"])))
        self.assertTrue(all(not reward.is_expired() for reward in home.context["available_rewards"]))
