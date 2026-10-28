from unittest import mock

from elasticsearch import NotFoundError

from citation.models import (
    AuditCommand,
    AuditLog,
    Author,
    Container,
    Publication,
    PublicationAuthors,
)
from citation.serializers import PublicationSerializer

from catalog.core.search_indexes import (
    _curator_contributor_data,
    refresh_search_documents,
)

from .common import BaseTest


class SearchSyncTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.container = Container.objects.create(name='Journal')
        self.author = Author.objects.create(
            given_name='Ada',
            family_name='Lovelace',
            email='private@example.com',
        )
        self.publication = Publication.objects.create(
            title='Searchable model',
            added_by=self.user,
            container=self.container,
            contact_email='contact@example.com',
            status=Publication.Status.REVIEWED,
        )
        PublicationAuthors.objects.create(
            publication=self.publication,
            author=self.author,
            role=PublicationAuthors.RoleChoices.AUTHOR,
        )

    def test_reviewed_publication_updates_public_and_curator_aliases_without_pii(self):
        client = mock.MagicMock()
        client.indices.get_alias.return_value = {'generation-1': {}}
        with mock.patch('catalog.core.search_indexes.get_es_client', return_value=client):
            refresh_search_documents(publication_ids=(self.publication.pk,))

        aliases = [call.kwargs['index'] for call in client.index.call_args_list]
        self.assertEqual(aliases[:2], ['publication_curator', 'publication'])
        # the publication's related records reach autocomplete with it
        related = {(call.kwargs['index'], call.kwargs['id']) for call in client.index.call_args_list[2:]}
        self.assertEqual(related, {('container', self.container.pk), ('author', self.author.pk)})
        public_document = client.index.call_args_list[1].kwargs['document']
        self.assertNotIn('contact_email', public_document)
        self.assertNotIn('has_contact_email', public_document)
        self.assertNotIn('email', public_document['authors'][0])
        # no per-document wait: one refresh of the touched aliases at the end makes it all visible
        self.assertTrue(all(call.kwargs['refresh'] is False for call in client.index.call_args_list))
        client.indices.refresh.assert_called_once_with(index='author,container,publication,publication_curator')

    def test_a_missing_alias_is_never_written_through(self):
        # client.index through a missing alias would auto-create a concrete index of that
        # name, after which rebuild_es_index can never add the alias again.
        client = mock.MagicMock()
        client.indices.get_alias.side_effect = NotFoundError('no such alias', meta=None, body=None)
        with (
            mock.patch('catalog.core.search_indexes.get_es_client', return_value=client),
            self.assertLogs('catalog.core.search_indexes', 'WARNING') as logs,
        ):
            refresh_search_documents(publication_ids=(self.publication.pk,))

        client.index.assert_not_called()
        self.assertTrue(any('run rebuild_es_index' in line for line in logs.output))

    def test_unreviewed_publication_is_removed_from_public_alias(self):
        self.publication.status = Publication.Status.UNREVIEWED
        self.publication.save(update_fields=('status',))
        client = mock.MagicMock()
        client.indices.get_alias.return_value = {'generation-1': {}}
        with mock.patch('catalog.core.search_indexes.get_es_client', return_value=client):
            refresh_search_documents(publication_ids=(self.publication.pk,))

        indexed = [call.kwargs['index'] for call in client.index.call_args_list]
        self.assertEqual(indexed[0], 'publication_curator')
        self.assertNotIn('publication', indexed)
        client.options.assert_called_once_with(ignore_status=[404])
        self.assertEqual(
            client.options.return_value.delete.call_args.kwargs['index'],
            'publication',
        )

    def test_serializer_save_survives_elasticsearch_outage(self):
        serializer = PublicationSerializer(self.publication)
        data = serializer.data
        data['title'] = 'Saved while search is unavailable'
        serializer = PublicationSerializer(self.publication, data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

        with mock.patch(
            'catalog.core.search_sync.refresh_search_documents',
            side_effect=RuntimeError('Elasticsearch unavailable'),
        ), self.assertLogs('citation.signals', level='ERROR'):
            with self.captureOnCommitCallbacks(execute=True):
                saved = serializer.save(user=self.user)

        saved.refresh_from_db()
        self.assertEqual(saved.title, 'Saved while search is unavailable')

    def test_curator_contributions_are_aggregated_for_all_publications(self):
        other_user = self.create_user(
            username='other-curator',
            email='other@example.com',
        )
        first_command = AuditCommand.objects.create(
            creator=self.user,
            action=AuditCommand.Action.MANUAL,
        )
        second_command = AuditCommand.objects.create(
            creator=other_user,
            action=AuditCommand.Action.MANUAL,
        )
        AuditLog.objects.bulk_create(
            [
                AuditLog(
                    action='UPDATE',
                    row_id=self.publication.pk,
                    table='publication',
                    audit_command=first_command,
                ),
                AuditLog(
                    action='UPDATE',
                    row_id=self.publication.pk,
                    table='publication',
                    audit_command=first_command,
                ),
                AuditLog(
                    action='UPDATE',
                    row_id=self.author.pk,
                    table='author',
                    pub_id=self.publication,
                    audit_command=second_command,
                ),
            ]
        )

        contributions = _curator_contributor_data()

        self.assertCountEqual(
            contributions[self.publication.pk],
            ['testcase (66%)', 'other-curator (33%)'],
        )
