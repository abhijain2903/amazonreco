"""Pull POs and receipts from Amazon's SP-API now.

    manage.py amazon_sync                 # since the last sync (or the last 7 days the first time)
    manage.py amazon_sync --days 180      # go-live backfill: Amazon keeps 6 months
    manage.py amazon_sync --test          # check the credentials only
"""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from integrations import amazon


class Command(BaseCommand):
    help = "Pull new and changed POs and receipts from Amazon (SP-API) and check sent documents."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, help="Look back this many days (at most 180)")
        parser.add_argument("--test", action="store_true", help="Only test the connection")

    def handle(self, *args, days=None, test=False, **opts):
        if test:
            ok, msg = amazon.test_connection()
            (self.stdout.write(msg) if ok else self.stderr.write(msg))
            return
        if not amazon.is_live():
            raise CommandError("The Amazon connector is not in API mode (Settings → Integrations → Amazon Vendor Central).")
        since = timezone.now() - timedelta(days=min(days, 180)) if days else None
        run = amazon.sync(since=since)
        (self.stdout.write(run.message) if run.ok else self.stderr.write(run.message))
