from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0003_accountrequest_applied_seq"),
    ]

    operations = [
        migrations.AddField(
            model_name="accountrequest",
            name="self_service",
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name="ClusterUser",
            fields=[
                ("username", models.CharField(max_length=128, primary_key=True, serialize=False)),
                ("active", models.BooleanField()),
                ("admin", models.BooleanField()),
                ("captured_at", models.DateTimeField()),
            ],
            options={"ordering": ["username"]},
        ),
    ]
