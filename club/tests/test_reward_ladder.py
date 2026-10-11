from django.test import TestCase

from club.models import LoyaltyMilestone

# The owner's confirmed ladder (a picture of the table they supplied), as seeded by migration 0013.
EXPECTED = {
    10: ("Dinker", [("DISCOUNT", "₱50 OFF one OpenPlay", "discount-50", 30)]),
    20: ("Dink Addict", [("DISCOUNT", "₱100 OFF one OpenPlay", "discount-100", 30)]),
    30: ("Court Regular", [("FREE_OPENPLAY", "FREE one OpenPlay", "free-openplay", 30)]),
    50: ("Got Cha Loyalist", [
        ("FREE_OPENPLAY", "FREE one OpenPlay", "free-openplay", 30),
        ("RECOGNITION", "Exclusive 50-OP recognition", None, None),
    ]),
    75: ("Club Veteran", [
        ("FREE_OPENPLAY", "FREE one OpenPlay", "free-openplay", 30),
        ("PERK", "Small exclusive club item/perk", "club-perk", 60),
    ]),
    100: ("Century Club", [
        ("RECOGNITION", "Special recognition", None, None),
        ("PREMIUM_REWARD", "Premium loyalty reward", "premium-reward", 60),
    ]),
}


class RewardLadderTests(TestCase):
    def test_the_active_ladder_is_exactly_the_owners_table(self):
        active = LoyaltyMilestone.objects.filter(is_active=True).order_by("threshold")
        self.assertEqual([m.threshold for m in active], sorted(EXPECTED))
        for milestone in active:
            name, benefits = EXPECTED[milestone.threshold]
            self.assertEqual(milestone.name, name, milestone.threshold)
            got = [(b["kind"], b["description"], b.get("key"), b.get("valid_days")) for b in milestone.benefits]
            self.assertEqual(got, benefits, milestone.threshold)

    def test_amounts_are_in_pesos_on_the_discounts(self):
        amounts = {m.threshold: m.benefits[0]["amount_php"] for m in LoyaltyMilestone.objects.filter(threshold__in=(10, 20))}
        self.assertEqual(amounts, {10: 50, 20: 100})

    def test_the_25_milestone_from_the_interim_ladder_is_retired_not_deleted(self):
        retired = LoyaltyMilestone.objects.get(threshold=25)
        self.assertFalse(retired.is_active)

    def test_every_milestone_still_passes_model_validation(self):
        for milestone in LoyaltyMilestone.objects.filter(is_active=True):
            milestone.full_clean()
