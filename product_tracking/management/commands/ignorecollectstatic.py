# product_tracking/management/commands/ignorecollectstatic.py

from django.core.management.base import BaseCommand
from django.core.management import call_command

class Command(BaseCommand):
    help = 'Collect static files and ignore missing files errors'

    def handle(self, *args, **options):
        try:
            call_command('collectstatic', interactive=False, ignore_errors=True)
        except Exception as e:
            self.stdout.write(self.style.WARNING(f'Ignored error during collectstatic: {e}'))
