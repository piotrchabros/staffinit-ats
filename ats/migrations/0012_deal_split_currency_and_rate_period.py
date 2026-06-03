"""Split Deal.currency into per-side salary_currency / client_rate_currency and
add rate_period (monthly/hourly). Existing single currency is copied into both
sides so no historical deal loses its currency."""

from django.db import migrations, models


def copy_currency(apps, schema_editor):
    Deal = apps.get_model("ats", "Deal")
    for deal in Deal.objects.all():
        deal.salary_currency = deal.currency
        deal.client_rate_currency = deal.currency
        deal.save(update_fields=["salary_currency", "client_rate_currency"])


def restore_currency(apps, schema_editor):
    # Reverse: collapse back to a single currency (use the salary side).
    Deal = apps.get_model("ats", "Deal")
    for deal in Deal.objects.all():
        deal.currency = deal.salary_currency
        deal.save(update_fields=["currency"])


class Migration(migrations.Migration):

    dependencies = [
        ("ats", "0011_company_is_archived"),
    ]

    operations = [
        migrations.AddField(
            model_name="deal",
            name="rate_period",
            field=models.CharField(
                choices=[("monthly", "Monthly"), ("hourly", "Hourly")],
                default="monthly",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="deal",
            name="salary_currency",
            field=models.CharField(default="PLN", max_length=8),
        ),
        migrations.AddField(
            model_name="deal",
            name="client_rate_currency",
            field=models.CharField(default="PLN", max_length=8),
        ),
        migrations.RunPython(copy_currency, restore_currency),
        migrations.RemoveField(
            model_name="deal",
            name="currency",
        ),
    ]
