# The display labels of two incident components; the stored values stay ha-general and ha-private.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0004_self_service_and_cluster_users"),
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
