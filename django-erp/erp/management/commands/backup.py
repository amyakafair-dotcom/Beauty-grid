"""python manage.py backup  ->  backups/backup-YYYYmmdd-HHMMSS.json.gz (keeps the newest 30).
Restore with: python manage.py loaddata backups/<file>.json.gz"""
import gzip
import io
from pathlib import Path

from django.conf import settings
from django.core.management import BaseCommand, call_command
from django.utils import timezone


class Command(BaseCommand):
    help = "Save all ERP data and logins to a compressed file."

    def add_arguments(self, parser):
        parser.add_argument("--dir", default=str(Path(settings.BASE_DIR) / "backups"))
        parser.add_argument("--keep", type=int, default=30)

    def handle(self, *args, dir, keep, **options):
        folder = Path(dir)
        folder.mkdir(parents=True, exist_ok=True)
        buf = io.StringIO()
        call_command("dumpdata", "auth.user", "erp", natural_foreign=True, indent=None, stdout=buf)
        path = folder / f"backup-{timezone.now():%Y%m%d-%H%M%S}.json.gz"
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(buf.getvalue())
        for old in sorted(folder.glob("backup-*.json.gz"))[:-keep]:
            old.unlink()
        self.stdout.write(f"Saved {path}")
