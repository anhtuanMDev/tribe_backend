from django.core.management.base import BaseCommand
from api.models import ActivityType
from api.constants import ACTIVITIES


class Command(BaseCommand):
    help = "Seed ActivityType table with default activities"

    def handle(self, *args, **kwargs):
        created_count = 0
        for name, icon_key in ACTIVITIES:
            _, created = ActivityType.objects.get_or_create(
                name=name,
                defaults={"icon_key": icon_key},
            )
            if created:
                created_count += 1
                self.stdout.write(f"  Created: {name}")
            else:
                self.stdout.write(f"  Skipped (exists): {name}")

        self.stdout.write(
            self.style.SUCCESS(f"\nDone. {created_count} new activities seeded.")
        )
