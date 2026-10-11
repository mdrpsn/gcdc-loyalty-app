from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.permissions import ADMIN, ORGANIZER
from club.loyalty import reconcile_player_loyalty, request_reward_redemption
from club.models import Attendance, OpenPlay, Player, RewardIssuance, RewardRedemptionRequest, Venue

PENDING = RewardRedemptionRequest.Status.PENDING


class RewardRequestViewTests(TestCase):
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
        self.other_user = User.objects.create_user("other@example.com", "Securepass123")
        Player.objects.create(user=self.other_user, display_name="Other")
        self.venue = Venue.objects.create(name="Main Court")
        self.now = timezone.now()
        past = OpenPlay.objects.bulk_create([
            OpenPlay(venue=self.venue, starts_at=self.now - timedelta(days=i + 2),
                     ends_at=self.now - timedelta(days=i + 2) + timedelta(hours=2),
                     status=OpenPlay.Status.COMPLETED,
                     completed_at=self.now - timedelta(days=i + 2) + timedelta(hours=2))
            for i in range(75)
        ])
        Attendance.objects.bulk_create([
            Attendance(player=self.player, openplay=session, source=Attendance.Source.HISTORICAL,
                       recorded_by=self.admin, checked_in_at=session.starts_at)
            for session in past
        ])
        reconcile_player_loyalty(self.player, actor=self.admin)
        self.discount = RewardIssuance.objects.get(player=self.player, benefit_key="discount-50")
        self.gear = RewardIssuance.objects.get(player=self.player, benefit_key="club-perk")
        self.soon = self.upcoming(1)

    def upcoming(self, days):
        return OpenPlay.objects.create(
            venue=self.venue, starts_at=self.now + timedelta(days=days),
            ends_at=self.now + timedelta(days=days, hours=2),
        )

    def use_url(self, issuance):
        return reverse("player:reward_use", args=[issuance.pk])

    def make_request(self):
        return request_reward_redemption(issuance_id=self.discount.pk, openplay_id=self.soon.pk, actor=self.member)

    def test_rewards_page_offers_use_only_for_session_bound_available_rewards(self):
        self.client.force_login(self.member)
        page = self.client.get(reverse("player:rewards"))
        self.assertContains(page, self.use_url(self.discount))
        self.assertNotContains(page, self.use_url(self.gear))  # gear is handed over by the club
        RewardIssuance.objects.filter(pk=self.discount.pk).update(
            earned_at=self.now - timedelta(days=40), expires_at=self.now - timedelta(days=10),
        )
        self.assertNotContains(self.client.get(reverse("player:rewards")), self.use_url(self.discount))

    def test_use_page_lists_only_upcoming_sessions_before_expiry_with_payment_guidance(self):
        far = self.upcoming(40)  # starts after the 30-day expiry
        OpenPlay.objects.create(  # already started
            venue=self.venue, starts_at=self.now - timedelta(hours=1), ends_at=self.now + timedelta(hours=1),
        )
        self.client.force_login(self.member)
        page = self.client.get(self.use_url(self.discount))
        self.assertEqual(list(page.context["form"].fields["openplay"].queryset), [self.soon])
        self.assertNotIn(far, page.context["form"].fields["openplay"].queryset)
        self.assertContains(page, "less ₱50")
        self.assertContains(page, "proof of payment")

    def test_player_requests_then_cancels_and_the_reward_stays_available(self):
        self.client.force_login(self.member)
        response = self.client.post(self.use_url(self.discount), {"openplay": self.soon.pk})
        self.assertRedirects(response, reverse("player:rewards"))
        request = RewardRedemptionRequest.objects.get()
        self.assertEqual((request.status, request.openplay), (PENDING, self.soon))
        page = self.client.get(reverse("player:rewards"))
        self.assertContains(page, "Waiting for an admin to confirm")
        self.assertNotContains(page, self.use_url(self.discount))
        self.discount.refresh_from_db()
        self.assertEqual(self.discount.status, RewardIssuance.Status.AVAILABLE)
        cancel = reverse("player:reward_request_cancel", args=[request.pk])
        self.assertEqual(self.client.get(cancel).status_code, 405)  # cancelling is POST only
        self.assertRedirects(self.client.post(cancel), reverse("player:rewards"))
        request.refresh_from_db()
        self.assertEqual(request.status, RewardRedemptionRequest.Status.CANCELLED)
        self.assertContains(self.client.get(reverse("player:rewards")), self.use_url(self.discount))

    def test_a_failed_request_shows_the_reason_and_creates_nothing(self):
        self.make_request()
        self.client.force_login(self.member)
        response = self.client.post(self.use_url(self.discount), {"openplay": self.soon.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already has an open request")
        self.assertEqual(RewardRedemptionRequest.objects.count(), 1)

    def test_players_cannot_touch_each_others_rewards_or_requests(self):
        request = self.make_request()
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(self.use_url(self.discount)).status_code, 404)
        self.assertEqual(self.client.post(reverse("player:reward_request_cancel", args=[request.pk])).status_code, 404)
        request.refresh_from_db()
        self.assertEqual(request.status, PENDING)

    def test_only_admins_see_the_queue_and_can_review(self):
        request = self.make_request()
        queue, review = reverse("organizer:reward_requests"), reverse("organizer:reward_request_review", args=[request.pk])
        self.client.force_login(self.organizer)
        self.assertEqual(self.client.get(queue).status_code, 403)
        self.assertEqual(self.client.post(review, {"action": "approve"}).status_code, 403)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(queue).status_code, 403)
        request.refresh_from_db()
        self.assertEqual(request.status, PENDING)

    def test_admin_queue_shows_what_to_collect_and_approves(self):
        request = self.make_request()
        self.client.force_login(self.admin)
        queue = reverse("organizer:reward_requests")
        page = self.client.get(queue)
        self.assertContains(page, "Requested (1)")
        self.assertContains(page, "The regular fee less ₱50")
        self.assertContains(page, "Main Court")
        review = reverse("organizer:reward_request_review", args=[request.pk])
        self.assertRedirects(self.client.post(review, {"action": "approve", "status": "pending"}),
                             f"{queue}?status=pending")
        request.refresh_from_db()
        self.discount.refresh_from_db()
        self.assertEqual(request.status, RewardRedemptionRequest.Status.APPROVED)
        self.assertEqual(self.discount.status, RewardIssuance.Status.REDEEMED)
        self.assertEqual(request.redemption.openplay, self.soon)
        self.assertContains(self.client.get(f"{queue}?status=redeemed"), "Redeemed (1)")

    def test_decline_needs_a_reason_and_release_frees_the_reward(self):
        request = self.make_request()
        self.client.force_login(self.admin)
        review = reverse("organizer:reward_request_review", args=[request.pk])
        response = self.client.post(review, {"action": "decline", "reason": " "}, follow=True)
        self.assertContains(response, "decline reason is required")
        request.refresh_from_db()
        self.assertEqual(request.status, PENDING)
        self.client.post(review, {"action": "decline", "reason": "No payment seen"})
        request.refresh_from_db()
        self.assertEqual((request.status, request.decline_reason),
                         (RewardRedemptionRequest.Status.DECLINED, "No payment seen"))
        again = self.make_request()
        self.client.post(reverse("organizer:reward_request_review", args=[again.pk]), {"action": "release"})
        again.refresh_from_db()
        self.assertEqual(again.status, RewardRedemptionRequest.Status.RELEASED)
        self.assertEqual(self.client.post(review, {"action": "bogus"}).status_code, 400)

    def test_session_page_lists_requested_and_redeemed_rewards_for_that_session_only(self):
        session_url = reverse("organizer:session", args=[self.soon.pk])
        other_url = reverse("organizer:session", args=[self.upcoming(4).pk])
        self.client.force_login(self.organizer)
        self.assertNotContains(self.client.get(session_url), "Rewards for this OpenPlay")
        request = self.make_request()
        page = self.client.get(session_url)
        self.assertContains(page, "Rewards for this OpenPlay")
        self.assertContains(page, "Collect the regular fee less ₱50")
        self.assertContains(page, "Requested")
        self.assertContains(page, "does not register the player")
        self.assertEqual(self.client.get(session_url).context["registered_count"], 0)  # no auto-registration
        self.assertNotContains(self.client.get(other_url), "Rewards for this OpenPlay")
        self.client.force_login(self.admin)
        self.client.post(reverse("organizer:reward_request_review", args=[request.pk]), {"action": "approve"})
        self.assertContains(self.client.get(session_url), "Redeemed")
        # a direct (cash at the court) redemption for the same session is listed once as well
        free = RewardIssuance.objects.get(player=self.player, milestone__threshold=50, benefit_key="free-openplay")
        from club.loyalty import redeem_reward
        redeem_reward(issuance_id=free.pk, player_id=self.player.pk, actor=self.organizer, openplay_id=self.soon.pk)
        page = self.client.get(session_url)
        self.assertContains(page, "Collect nothing: free OpenPlay")
        self.assertEqual(len(page.context["reward_rows"]), 2)

    def test_cancelled_and_declined_requests_are_not_listed_on_the_session(self):
        request = self.make_request()
        self.client.force_login(self.admin)
        self.client.post(reverse("organizer:reward_request_review", args=[request.pk]), {"action": "release"})
        self.assertNotContains(self.client.get(reverse("organizer:session", args=[self.soon.pk])),
                               "Rewards for this OpenPlay")

    def test_staff_see_an_open_request_on_the_players_record(self):
        self.make_request()
        self.client.force_login(self.organizer)
        page = self.client.get(reverse("organizer:player", args=[self.player.pk]))
        self.assertContains(page, "Player requested this for")
        self.assertNotContains(page, "Reward requests")  # the queue itself is admin-only
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("organizer:player", args=[self.player.pk])), "Reward requests")
