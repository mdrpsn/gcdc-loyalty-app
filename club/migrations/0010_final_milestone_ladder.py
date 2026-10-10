from django.db import migrations


# Redeemable benefits carry a stable "key" and an optional "valid_days" (counted from the day the
# reward is earned). RECOGNITION is status shown from the attendance count; it never expires.
FREE_OP = {"kind": "FREE_OPENPLAY", "key": "free-openplay", "description": "FREE one OpenPlay", "valid_days": 30}

LADDER = (
    (10, "Dinker", [
        {"kind": "DISCOUNT", "key": "discount-50", "description": "₱50 OFF one OpenPlay",
         "amount_php": 50, "currency": "PHP", "valid_days": 30},
        {"kind": "RECOGNITION", "description": "10-OP milestone recognition"},
    ]),
    (25, "Dink Addict", [
        {"kind": "RECOGNITION", "description": "25-OP milestone recognition"},
    ]),
    (50, "Got Cha Loyalist", [
        dict(FREE_OP),
        {"kind": "RECOGNITION", "description": "50-OP recognition"},
    ]),
    (75, "Club Veteran", [
        dict(FREE_OP),
        {"kind": "PERK", "key": "overgrip", "description": "1 overgrip", "valid_days": 60},
        {"kind": "RECOGNITION", "description": "75-OP recognition"},
    ]),
    (100, "Century Club", [
        dict(FREE_OP),
        {"kind": "PREMIUM_REWARD", "key": "century-shirt",
         "description": "Exclusive Got Cha Dink Club Century Club shirt", "valid_days": 60},
        {"kind": "RECOGNITION", "description": "Century Club recognition and status"},
    ]),
)
RETIRED_THRESHOLDS = (20, 30)


def apply_ladder(apps, schema_editor):
    Milestone = apps.get_model("club", "LoyaltyMilestone")
    for threshold, name, benefits in LADDER:
        Milestone.objects.update_or_create(
            threshold=threshold, defaults={"name": name, "benefits": benefits, "is_active": True}
        )
    # Retired, not deleted: rewards already issued keep their snapshotted terms and history.
    Milestone.objects.filter(threshold__in=RETIRED_THRESHOLDS).update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [("club", "0009_reward_benefit_expiry")]
    operations = [migrations.RunPython(apply_ladder, migrations.RunPython.noop)]
