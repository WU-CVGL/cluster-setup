from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0002_admin_console"),
    ]

    operations = [
        migrations.AddField(
            model_name="accountrequest",
            name="applied_seq",
            field=models.BigIntegerField(blank=True, null=True),
        ),
    ]
