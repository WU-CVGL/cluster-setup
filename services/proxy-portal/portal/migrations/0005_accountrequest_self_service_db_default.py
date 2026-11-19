from django.db import migrations, models


class Migration(migrations.Migration):
    """A database default for self_service, so the previous release's code can still create requests."""

    dependencies = [
        ("portal", "0004_self_service_and_cluster_users"),
    ]

    operations = [
        migrations.AlterField(
            model_name="accountrequest",
            name="self_service",
            field=models.BooleanField(default=False, db_default=False),
        ),
    ]
