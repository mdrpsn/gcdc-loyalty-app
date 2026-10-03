from django.db import migrations


LEVELS = (
    ("ROOKIE", "Rookie", 0),
    ("REGULAR", "Regular", 5),
    ("PLAYER", "Player", 10),
    ("COURT_REGULAR", "Court Regular", 20),
    ("LEGEND", "Legend", 30),
)

MILESTONES = (
    (10, "Dinker", [
        {"kind": "DISCOUNT", "description": "₱50 OFF one OpenPlay", "amount_php": 50, "currency": "PHP"},
    ]),
    (20, "Dink Addict", [
        {"kind": "DISCOUNT", "description": "₱100 OFF one OpenPlay", "amount_php": 100, "currency": "PHP"},
    ]),
    (30, "Court Regular", [
        {"kind": "FREE_OPENPLAY", "description": "FREE one OpenPlay"},
    ]),
    (50, "Got Cha Loyalist", [
        {"kind": "FREE_OPENPLAY", "description": "FREE one OpenPlay"},
        {"kind": "RECOGNITION", "description": "Exclusive 50-OP recognition"},
    ]),
    (75, "Club Veteran", [
        {"kind": "FREE_OPENPLAY", "description": "FREE one OpenPlay"},
        {"kind": "PERK", "description": "Small exclusive club item/perk"},
    ]),
    (100, "Century Club", [
        {"kind": "RECOGNITION", "description": "Special recognition"},
        {"kind": "PREMIUM_REWARD", "description": "Premium loyalty reward"},
    ]),
)


def seed_loyalty(apps, schema_editor):
    Level = apps.get_model("club", "LoyaltyLevel")
    Milestone = apps.get_model("club", "LoyaltyMilestone")
    alias = schema_editor.connection.alias
    for code, name, minimum in LEVELS:
        Level.objects.using(alias).get_or_create(
            code=code, defaults={"name": name, "minimum_attendances": minimum}
        )
    for threshold, name, benefits in MILESTONES:
        Milestone.objects.using(alias).get_or_create(
            threshold=threshold, defaults={"name": name, "benefits": benefits}
        )


class Migration(migrations.Migration):
    dependencies = [("club", "0003_loyaltylevel_loyaltymilestone_rewardissuance_and_more")]

    # Removing configuration after entitlements exist would destroy history.
    operations = [migrations.RunPython(seed_loyalty, migrations.RunPython.noop)]
