# The display labels of two incident components; the stored values stay ha-general and ha-private.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0005_accountrequest_self_service_db_default"),
    ]

    operations = [
        migrations.AlterField(
            model_name="incident",
            name="component",
            field=models.CharField(choices=[("ha-general", "Auto-switching Proxy"),
                                            ("ha-private", "Private Auto-switching Proxy"),
                                            ("proxies", "Individual proxies"), ("monitoring", "Monitoring"),
                                            ("portal", "Portal")], max_length=32),
        ),
    ]
