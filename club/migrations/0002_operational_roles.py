from django.db import migrations


def create_operational_roles(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    for name in ("ORGANIZER", "ADMIN"):
        Group.objects.using(schema_editor.connection.alias).get_or_create(name=name)


class Migration(migrations.Migration):
    dependencies = [("club", "0001_initial")]

    operations = [migrations.RunPython(create_operational_roles, migrations.RunPython.noop)]
