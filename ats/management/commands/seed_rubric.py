"""Create and activate a default v1 rubric if none is active.

A convenience for getting started — the real rubric should come from the
office-hours assignment (reverse-engineered from 3 real roles). Run:

    uv run python manage.py seed_rubric
"""

from django.core.management.base import BaseCommand

from ats.models import Rubric

DEFAULT_CRITERIA = [
    {"name": "Technical fit", "scale": 5, "weight": 0.45,
     "description": "Depth and recency of the required technical skills."},
    {"name": "Domain experience", "scale": 5, "weight": 0.30,
     "description": "Relevant industry / problem-domain experience."},
    {"name": "Seniority", "scale": 5, "weight": 0.25,
     "description": "Years of experience and scope/ownership."},
]


class Command(BaseCommand):
    help = "Create and activate a default v1 rubric if none is active."

    def handle(self, *args, **options):
        if Rubric.active():
            self.stdout.write("An active rubric already exists; nothing to do.")
            return
        latest = Rubric.objects.order_by("-version").first()
        next_version = (latest.version + 1) if latest else 1
        rubric = Rubric.objects.create(
            version=next_version,
            name="Default rubric",
            criteria=DEFAULT_CRITERIA,
            is_active=True,
        )
        self.stdout.write(self.style.SUCCESS(f"Created and activated Rubric v{rubric.version}."))
