from django.db import migrations, models


class Migration(migrations.Migration):
    """The private proxy of a grant or revoke request: nullable, so the previous release's code still creates
    requests."""

    dependencies = [
        ("portal", "0006_incident_component_labels"),
    ]

    operations = [
        migrations.AddField(
            model_name="accountrequest",
            name="proxy",
            field=models.CharField(blank=True, max_length=128, null=True),
        ),
        migrations.AlterField(
            model_name="accountrequest",
            name="op",
            field=models.CharField(choices=[("add", "Add"), ("rotate", "New password"), ("enable", "Turn on"),
                                            ("disable", "Turn off"), ("remove", "Remove"),
                                            ("admin-on", "Admin account on"), ("admin-off", "Admin account off"),
                                            ("grant", "Private proxy on"), ("revoke", "Private proxy off")],
                                   max_length=16),
        ),
    ]
