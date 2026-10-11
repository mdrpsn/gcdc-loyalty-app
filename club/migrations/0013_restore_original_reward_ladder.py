from django.db import migrations


# The owner's confirmed ladder, supplied as a table: 10/20/30/50/75/100. It replaces the interim ladder in
# migration 0010 (which dropped 20 and 30 and added 25). Redeemable benefits carry a stable "key" and a
# "valid_days" counted from the day the reward is earned; RECOGNITION is a status shown from the
# attendance count and never expires. The 75 and 100 items are the owner's placeholders ("small exclusive
# club item/perk", "premium loyalty reward"); staff decide the actual item when it is redeemed.
FREE_OP = {"kind": "FREE_OPENPLAY", "key": "free-openplay", "description": "FREE one OpenPlay", "valid_days": 30}

LADDER = (
    (10, "Dinker", [
        {"kind": "DISCOUNT", "key": "discount-50", "description": "₱50 OFF one OpenPlay",
         "amount_php": 50, "currency": "PHP", "valid_days": 30},
    ]),
    (20, "Dink Addict", [
        {"kind": "DISCOUNT", "key": "discount-100", "description": "₱100 OFF one OpenPlay",
         "amount_php": 100, "currency": "PHP", "valid_days": 30},
    ]),
    (30, "Court Regular", [
        dict(FREE_OP),
    ]),
    (50, "Got Cha Loyalist", [
        dict(FREE_OP),
        {"kind": "RECOGNITION", "description": "Exclusive 50-OP recognition"},
    ]),
    (75, "Club Veteran", [
        dict(FREE_OP),
        {"kind": "PERK", "key": "club-perk", "description": "Small exclusive club item/perk", "valid_days": 60},
    ]),
    (100, "Century Club", [
        {"kind": "RECOGNITION", "description": "Special recognition"},
        {"kind": "PREMIUM_REWARD", "key": "premium-reward", "description": "Premium loyalty reward",
         "valid_days": 60},
    ]),
)
RETIRED_THRESHOLDS = (25,)


def apply_ladder(apps, schema_editor):
    Milestone = apps.get_model("club", "LoyaltyMilestone")
    for threshold, name, benefits in LADDER:
        Milestone.objects.update_or_create(
            threshold=threshold, defaults={"name": name, "benefits": benefits, "is_active": True}
        )
    # Retired, not deleted: rewards already issued keep their snapshotted terms and history.
    Milestone.objects.filter(threshold__in=RETIRED_THRESHOLDS).update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [("club", "0012_openplay_name")]
    operations = [migrations.RunPython(apply_ladder, migrations.RunPython.noop)]
