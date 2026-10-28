from datetime import datetime, timezone

from django.core.management.base import BaseCommand, CommandError

from citation.models import Publication

from catalog.core.search_indexes import refresh_search_documents

CHUNK = 500


class Command(BaseCommand):
    help = ('Re-index the publications modified since a moment, so search generations built before '
            'then (for example a rollback release\'s) catch up with the database')

    def add_arguments(self, parser):
        parser.add_argument('--since', required=True, help='UTC instant, ISO 8601, for example 2026-10-27T20:15:36Z')

    def handle(self, *args, **options):
        try:
            since = datetime.fromisoformat(options['since'].replace('Z', '+00:00'))
        except ValueError as exc:
            raise CommandError('--since must be an ISO 8601 instant: {0}'.format(exc)) from exc
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        ids = list(Publication.objects.filter(date_modified__gte=since).order_by('pk').values_list('pk', flat=True))
        for start in range(0, len(ids), CHUNK):
            refresh_search_documents(publication_ids=ids[start:start + CHUNK])
        self.stdout.write('{0} publications re-indexed since {1}'.format(len(ids), since.isoformat()))
