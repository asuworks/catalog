from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from citation.models import Container, Publication

from .common import BaseTest

REFRESH = 'catalog.core.management.commands.resync_search_documents.refresh_search_documents'


class ResyncSearchDocumentsCommandTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.publication = Publication.objects.create(
            title='Edited under the newer release', added_by=self.user,
            container=Container.objects.create(name='Journal'))

    def test_publications_modified_since_the_moment_are_refreshed(self):
        since = (timezone.now() - timedelta(minutes=5)).isoformat().replace('+00:00', 'Z')
        out = StringIO()
        with mock.patch(REFRESH) as refresh:
            call_command('resync_search_documents', '--since', since, stdout=out)
        refresh.assert_called_once_with(publication_ids=[self.publication.pk])
        self.assertIn('1 publications re-indexed', out.getvalue())

    def test_nothing_modified_since_the_moment_means_no_indexing(self):
        since = (timezone.now() + timedelta(minutes=5)).isoformat().replace('+00:00', 'Z')
        with mock.patch(REFRESH) as refresh:
            call_command('resync_search_documents', '--since', since, stdout=StringIO())
        refresh.assert_not_called()

    def test_the_moment_must_be_an_instant(self):
        with self.assertRaisesRegex(CommandError, 'ISO 8601'):
            call_command('resync_search_documents', '--since', 'yesterday', stdout=StringIO())
