from collections import defaultdict
from datetime import datetime, timezone
import logging
from urllib.parse import urlencode

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import BigIntegerField, Case, Count, F, Max, Q, When
from django.http import QueryDict
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from elasticsearch import NotFoundError
from elasticsearch.helpers import bulk
from elasticsearch_dsl import Document, InnerDoc, aggs, analyzer, connections, query, tokenizer
import elasticsearch_dsl as edsl

from citation.models import (
    Author,
    AuditLog,
    CodeArchiveUrl,
    Container,
    ModelDocumentation,
    Platform,
    Publication,
    Sponsor,
    Tag,
)

ALL_DATA_FIELD = 'all_data'
INDEX_SETTINGS = {'number_of_shards': 1, 'number_of_replicas': 0}

logger = logging.getLogger(__name__)


_ES_CLIENT = None


def get_es_client():
    """
    Lazily build (and cache) the Elasticsearch 8 client from
    ``settings.ELASTICSEARCH`` and register it as the elasticsearch-dsl
    ``default`` connection.

    No Elasticsearch configuration happens at Django startup: the client
    is only constructed on first use, and ``connections.configure`` merely
    stores options (no network I/O).
    """
    global _ES_CLIENT
    if _ES_CLIENT is None:
        connections.configure(default=dict(settings.ELASTICSEARCH))
        _ES_CLIENT = connections.get_connection()
    return _ES_CLIENT


##########################################
#     Generation-based index rebuilds    #
##########################################
#
# Reads always go through stable aliases (the ``Index.name`` of each doc
# class: publication, publication_curator, and the autocomplete aliases).
# Rebuilds write to a fresh generation index per alias
# (``<alias>-<utc-stamp>``) and validate each one; only after *every*
# generation validates are the stable aliases moved onto the new
# generations in one atomic multi-alias ``update_aliases`` call. The
# previous generation of each alias is retained for rollback; anything
# older is pruned.

class SearchRebuildError(Exception):
    """Raised when a generation index cannot be built or validated."""


def _utc_generation_timestamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dt%H%M%S%fz')


def generation_index_name(alias):
    return '{0}-{1}'.format(alias, _utc_generation_timestamp())


def _alias_target(client, alias):
    """Return the physical index currently serving ``alias`` (or None)."""
    try:
        return next(iter(client.indices.get_alias(name=alias)))
    except NotFoundError:
        return None


def _index_doc_count(client, index_name):
    # The ES8 client returns a mapping-backed response object
    # (``elastic_transport.ObjectApiResponse``) whose body is a plain
    # dict, so the count must be read with mapping access. Attribute
    # access on that object falls through to the raw body dict and
    # raises ``AttributeError`` for a ``count`` key.
    response = client.count(index=index_name)
    return response.get('count')


def _delete_index_quietly(client, index_name):
    # The ES8 client takes ignore_status as a request option, not a kwarg.
    client.options(ignore_status=[400, 404]).indices.delete(index=index_name)


def _create_generation_index(client, doc_class, index_name):
    # ``clone()`` carries the doc class's mappings, analyzers and index
    # settings (e.g. number_of_shards) onto the generation name.
    gen_index = doc_class._index.clone(name=index_name)
    client.indices.create(index=index_name, body=gen_index.to_dict())


def _prune_old_generations(client, alias, keep):
    """Delete generation indices of ``alias`` that are not in ``keep``."""
    keep = {name for name in keep if name}
    try:
        existing = client.indices.get(index='{0}-*'.format(alias))
    except NotFoundError:
        return
    for name in existing:
        if name not in keep:
            _delete_index_quietly(client, name)


def _force_actions_to_generation_index(documents):
    """
    Yield bulk actions with any per-action ``_index`` removed.

    Actions built with ``Document.to_dict(include_meta=True)`` (see the
    ``from_instance`` classmethods) stamp the stable read *alias* onto
    ``_index``. If that survives into the bulk request,
    ``elasticsearch.helpers.bulk`` honors the per-action index and the
    documents land in whatever the alias currently points at (the
    *previous* generation) instead of the new one. Stripping the key
    forces every action onto the ``index=`` argument of the bulk call.
    """
    for action in documents:
        action = dict(action)
        action.pop('_index', None)
        yield action


def build_document_generation(client, doc_class, documents, expected_count):
    """
    Build and validate one fresh generation index for ``doc_class``.

    Creates ``<alias>-<utc-stamp>`` carrying the doc class mappings
    (including the ``InnerDoc`` definitions for the embedded
    Object/Nested fields) and the single-node index settings, forces
    every bulk action onto that physical generation index, refreshes
    it, and validates the document count against the ES8 count
    response.

    ``documents`` is an iterable of ready-to-bulk action dicts. Any
    bulk error (``elasticsearch.helpers.bulk`` raises ``BulkIndexError``
    on partial failure by default) or a document-count mismatch deletes
    the new generation and re-raises. Stable aliases are never touched
    here: swapping is done by ``swap_generation_aliases``.

    Returns the new generation index name.
    """
    alias = doc_class._index._name
    index_name = generation_index_name(alias)
    created = False
    try:
        _create_generation_index(client, doc_class, index_name)
        created = True
        bulk(client=client,
             actions=_force_actions_to_generation_index(documents),
             index=index_name)
        client.indices.refresh(index=index_name)
        actual_count = _index_doc_count(client, index_name)
        if actual_count != expected_count:
            raise SearchRebuildError(
                'index {0} validation failed: expected {1} documents, found {2}'.format(
                    index_name, expected_count, actual_count))
    except BaseException:
        if created:
            _delete_index_quietly(client, index_name)
        raise
    return index_name


def swap_generation_aliases(client, alias_to_index):
    """
    Point every stable read alias at its new generation index.

    A single atomic ``update_aliases`` call removes each alias from its
    current target (if any) and adds it to the new generation, so
    readers see either the old set of generations or the new set, never
    a mix. After the swap, generation indices older than the retained
    previous generation are pruned per alias (pruning failures are
    logged and do not roll back the completed swap).
    """
    actions = []
    previous = {}
    for alias, index_name in alias_to_index.items():
        current = _alias_target(client, alias)
        if current is not None:
            previous[alias] = current
            actions.append({'remove': {'index': current, 'alias': alias}})
        actions.append({'add': {'index': index_name, 'alias': alias}})
    client.indices.update_aliases(actions=actions)
    for alias in alias_to_index:
        try:
            _prune_old_generations(client, alias,
                                   keep=(alias_to_index[alias], previous.get(alias)))
        except Exception:
            logger.exception('failed to prune old generations for alias %s', alias)
    return alias_to_index


def rebuild_document_indices(client, builds):
    """
    Rebuild every read alias using fresh generation indices.

    ``builds`` is an iterable of ``(doc_class, documents, expected_count)``
    triples. Every generation is built and validated *before* any alias
    moves: if any build or validation fails, no alias is touched and
    only the new generations that were created are deleted. Only once
    all generations validate are the stable aliases swapped onto them
    in one atomic multi-alias operation; a failed swap likewise leaves
    the live aliases untouched and cleans up only the new generations.
    Previous generations are always retained for rollback.

    Returns a mapping of alias -> new generation index name.
    """
    built = {}
    swapped = False
    try:
        for doc_class, documents, expected_count in builds:
            built[doc_class._index._name] = build_document_generation(
                client, doc_class, documents, expected_count)
        swap_generation_aliases(client, built)
        swapped = True
    except BaseException:
        if not swapped:
            for index_name in built.values():
                _delete_index_quietly(client, index_name)
        raise
    return built


class AuthorInnerDoc(InnerDoc):
    id = edsl.Integer(required=True)
    orcid = edsl.Keyword()
    researcherid = edsl.Keyword()
    name = edsl.Text(copy_to=ALL_DATA_FIELD)


class CodeArchiveUrlInnerDoc(InnerDoc):
    id = edsl.Integer(required=True)
    url = edsl.Text(copy_to=ALL_DATA_FIELD)
    status = edsl.Keyword()


class ContainerInnerDoc(InnerDoc):
    id = edsl.Integer(required=True)
    name = edsl.Text(copy_to=ALL_DATA_FIELD)
    issn = edsl.Keyword()


class RelatedInnerDoc(InnerDoc):
    id = edsl.Integer(required=True)
    name = edsl.Text(copy_to=ALL_DATA_FIELD)


def normalize_search_querydict(qd: QueryDict):
    search = qd.get('search', '')
    field_names_lookup = PublicationDocSearch.get_filter_field_names()
    filters = {}
    for field_name in field_names_lookup:
        filters[field_name] = set(int(ident) for ident in qd.getlist(field_name))
    return search, filters


class TopHits:
    def __init__(self, iterable, hits):
        self.iterable = iterable
        self.hits = hits

    def __iter__(self):
        return iter(self.iterable)


class AbstractAgg:
    def __init__(self, name):
        self.name = name

    def extract(self, response, ids):
        data = self.extract_count(response, ids)
        return {self.name: {'count': data}}


# Use top hits elasticsearch aggregator to avoid hitting DB
class UnnestedAgg(AbstractAgg):
    @property
    def _terms_bucket_name(self):
        return 'top_{}_count'.format(self.name)

    _top_hit_bucket_name = 'top_hit'

    def count(self, search):
        search.aggs.bucket(self._terms_bucket_name,
                           aggs.Terms(field='{}.id'.format(self.name))) \
            .bucket(self._top_hit_bucket_name,
                    aggs.TopHits(size=1, _source={'includes': [self.name]}))

    def extract_count(self, response, ids):
        term_buckets = response.aggs[self._terms_bucket_name].buckets
        results = []
        for bucket in term_buckets:
            result = {'publication_count': bucket.doc_count}
            result.update(bucket[self._top_hit_bucket_name].hits.hits[0]['_source'][self.name])
            result['checked'] = result['id'] in ids
            results.append(result)
        return results


class NestedAgg(AbstractAgg):
    @property
    def _top_bucket_name(self):
        return '{}'.format(self.name)

    _terms_bucket_name = 'top_count'
    _top_hit_bucket_name = 'top_hit'

    def count(self, search):
        search.aggs.bucket(self._top_bucket_name, aggs.Nested(path=self.name)) \
            .bucket(self._terms_bucket_name,
                    aggs.Terms(field='{}.id'.format(self.name))) \
            .bucket(self._top_hit_bucket_name, aggs.TopHits(size=1, _source={'includes': [self.name]}))

    def extract_count(self, response, ids):
        term_buckets = response.aggs[self._top_bucket_name][self._terms_bucket_name].buckets
        results = []
        for bucket in term_buckets:
            result = {'publication_count': bucket.doc_count}
            result.update(bucket[self._top_hit_bucket_name].hits.hits[0]['_source'])
            result['checked'] = result['id'] in ids
            results.append(result)
        return results


class FilterQuery:
    def __init__(self, name):
        self.field = '{}.id'.format(name)

    def by_ids(self, ids):
        return query.Q('terms', **{self.field: list(ids)})


class NestedFilterQuery:
    def __init__(self, name):
        self.path = name
        self.field = '{}.id'.format(name)

    def by_ids(self, ids):
        return query.Nested(path=self.path, query=query.Q('terms', **{self.field: list(ids)}))


class PublicationDocSearch:
    AUTHOR_FIELD_NAME = 'authors'
    CONTAINER_FIELD_NAME = 'container'
    PLATFORM_FIELD_NAME = 'platforms'
    SPONSOR_FIELD_NAME = 'sponsors'
    TAG_FIELD_NAME = 'tags'

    aggs = {
        AUTHOR_FIELD_NAME: NestedAgg(AUTHOR_FIELD_NAME),
        CONTAINER_FIELD_NAME: UnnestedAgg(CONTAINER_FIELD_NAME),
        PLATFORM_FIELD_NAME: NestedAgg(PLATFORM_FIELD_NAME),
        SPONSOR_FIELD_NAME: NestedAgg(SPONSOR_FIELD_NAME),
        TAG_FIELD_NAME: NestedAgg(TAG_FIELD_NAME)
    }

    filters = {
        AUTHOR_FIELD_NAME: NestedFilterQuery(AUTHOR_FIELD_NAME),
        CONTAINER_FIELD_NAME: FilterQuery(CONTAINER_FIELD_NAME),
        PLATFORM_FIELD_NAME: NestedFilterQuery(PLATFORM_FIELD_NAME),
        SPONSOR_FIELD_NAME: NestedFilterQuery(SPONSOR_FIELD_NAME),
        TAG_FIELD_NAME: NestedFilterQuery(TAG_FIELD_NAME)
    }

    def __init__(self, search=None, cache=None):
        self.search = PublicationDoc.search() if search is None else search
        self.cache = {} if cache is None else cache

    def __getitem__(self, val):
        return PublicationDocSearch(self.search[val])

    def _full_text(self, q):
        return query.QueryString(**{'query': q, 'default_field': ALL_DATA_FIELD})

    def _filter(self, facet_filters: dict[str, list[int]]):
        queries = []
        for field_name in facet_filters:
            ids = facet_filters[field_name]
            if ids:
                queries.append(self.filters[field_name].by_ids(ids))
        return queries

    def find(self, q, facet_filters):
        logger.info('filters: %s', facet_filters)
        queries = self._filter(facet_filters)
        full_text = self._full_text(q) if q else query.MatchAll()
        if queries:
            return PublicationDocSearch(self.search.query(
                query.Bool(should=queries, must=[full_text], minimum_should_match=1)))
        elif q:
            return PublicationDocSearch(self.search.query(full_text))
        else:
            return PublicationDocSearch(self.search.sort('-incomplete_date_published'))

    def source(self, fields=None, **kwargs):
        return PublicationDocSearch(self.search.source(fields=fields, **kwargs))

    def scan(self):
        # ensure the dsl default connection is configured (no network I/O)
        get_es_client()
        return self.search.scan()

    def agg_by_count(self):
        s = self.search._clone()
        for agg in self.aggs.values():
            agg.count(s)
        return PublicationDocSearch(s)

    @classmethod
    def get_filter_field_names(cls):
        return [cls.AUTHOR_FIELD_NAME, cls.CONTAINER_FIELD_NAME,
                cls.PLATFORM_FIELD_NAME, cls.SPONSOR_FIELD_NAME, cls.TAG_FIELD_NAME]

    def execute(self, facet_filters):
        # ensure the dsl default connection is configured (no network I/O)
        get_es_client()
        response = self.search.execute()
        for name in self.aggs:
            ids = facet_filters.get(name, [])
            agg = self.aggs[name]
            self.cache.update(agg.extract(response, ids))
        return response


def _publication_document_data(publication):
    container = publication.container
    code_archive_urls = list(publication.code_archive_urls.all())
    return {
        'id': publication.id,
        'title': publication.title,
        'abstract': publication.abstract,
        'date_published': publication.date_published,
        'incomplete_date_published': publication.incomplete_date_published,
        'last_modified': publication.date_modified,
        'code_archive_urls': [
            CodeArchiveUrlInnerDoc(id=item.id, url=item.url, status=item.status)
            for item in code_archive_urls
        ],
        'doi': publication.doi,
        'container': ContainerInnerDoc(
            id=container.id,
            name=container.name,
            issn=container.issn,
        ),
        'tags': [RelatedInnerDoc(id=item.id, name=item.name) for item in publication.tags.all()],
        'sponsors': [
            RelatedInnerDoc(id=item.id, name=item.name)
            for item in publication.sponsors.all()
        ],
        'platforms': [
            RelatedInnerDoc(id=item.id, name=item.name)
            for item in publication.platforms.all()
        ],
        'model_documentation': [
            RelatedInnerDoc(id=item.id, name=item.name)
            for item in publication.model_documentation.all()
        ],
        'authors': [
            AuthorInnerDoc(
                id=item.id,
                name=item.name,
                orcid=item.orcid,
                researcherid=item.researcherid,
            )
            for item in publication.creators.all()
        ],
        'is_archived': any(
            item.status != CodeArchiveUrl.STATUS.unavailable
            for item in code_archive_urls
        ),
    }


class PublicationDocument(Document):
    all_data = edsl.Text()
    id = edsl.Integer(required=True)
    title = edsl.Text(copy_to=ALL_DATA_FIELD)
    abstract = edsl.Text(copy_to=ALL_DATA_FIELD)
    date_published = edsl.Date()
    incomplete_date_published = edsl.Keyword()
    last_modified = edsl.Date()
    code_archive_urls = edsl.Nested(CodeArchiveUrlInnerDoc)
    doi = edsl.Keyword()
    container = edsl.Object(ContainerInnerDoc)
    tags = edsl.Nested(RelatedInnerDoc)
    sponsors = edsl.Nested(RelatedInnerDoc)
    platforms = edsl.Nested(RelatedInnerDoc)
    model_documentation = edsl.Nested(RelatedInnerDoc)
    authors = edsl.Nested(AuthorInnerDoc)
    is_archived = edsl.Boolean()

    @property
    def pk(self):
        return self.id

    @classmethod
    def _matches(cls, hit):
        index_name = hit.get('_index', '')
        alias = cls._index._name
        return index_name == alias or index_name.startswith('{0}-'.format(alias))

    class Meta:
        abstract = True


class PublicationDoc(PublicationDocument):

    @classmethod
    def from_instance(cls, publication):
        doc = cls(meta={'id': publication.id}, **_publication_document_data(publication))
        return doc.to_dict(include_meta=True)

    def get_public_detail_url(self):
        return reverse('core:public-publication-detail', kwargs={'pk': self.meta.id})

    @classmethod
    def get_breadcrumb_data(cls):
        return {'breadcrumb_trail': [
            {'link': reverse('core:public-home'), 'text': 'Home'},
            {'text': 'Publications'}
        ]}

    @classmethod
    def get_public_list_url(cls, search=None):
        location = reverse('core:public-search')
        if search:
            query_string = urlencode({'search': search})
            location += '?{}'.format(query_string)
        return location

    class Index:
        name = 'publication'
        settings = INDEX_SETTINGS


class CuratorPublicationDoc(PublicationDocument):
    status = edsl.Keyword()
    has_contact_email = edsl.Boolean()
    assigned_curator = edsl.Keyword()
    assigned_curator_id = edsl.Integer()
    flagged = edsl.Boolean()
    contributor_data = edsl.Keyword()

    @classmethod
    def from_instance(cls, publication, contributor_data=None):
        assigned_curator = publication.assigned_curator
        if contributor_data is None:
            contributor_data = [
                '{0} ({1})%'.format(item['creator'], item['contribution'])
                for item in publication.contributor_data()
            ]
        doc = cls(
            meta={'id': publication.id},
            status=publication.status,
            has_contact_email=bool(publication.contact_email),
            assigned_curator=assigned_curator.username if assigned_curator else '',
            assigned_curator_id=assigned_curator.id if assigned_curator else None,
            flagged=publication.flagged,
            contributor_data=contributor_data,
            **_publication_document_data(publication),
        )
        return doc.to_dict(include_meta=True)

    class Index:
        name = 'publication_curator'
        settings = INDEX_SETTINGS


def build_curator_publication_search(cleaned_data, search=None):
    search = CuratorPublicationDoc.search() if search is None else search
    must = []
    filters = []

    text = cleaned_data.get('q') or ''
    if text:
        must.append(query.QueryString(query=text, default_field=ALL_DATA_FIELD))
    if cleaned_data.get('publication_start_date'):
        filters.append(query.Range(
            date_published={'gte': cleaned_data['publication_start_date']}))
    if cleaned_data.get('publication_end_date'):
        filters.append(query.Range(
            date_published={'lte': cleaned_data['publication_end_date']}))
    if cleaned_data.get('status'):
        filters.append(query.Term(status=cleaned_data['status']))
    if cleaned_data.get('journal'):
        filters.append(query.MatchPhrase(**{
            'container.name': cleaned_data['journal'],
        }))

    for tag_name in cleaned_data.get('tags') or []:
        filters.append(query.Nested(
            path='tags',
            query=query.MatchPhrase(**{'tags.name': tag_name}),
        ))
    if cleaned_data.get('authors'):
        filters.append(query.Nested(
            path='authors',
            query=query.MatchPhrase(**{'authors.name': cleaned_data['authors']}),
        ))
    if cleaned_data.get('assigned_curator'):
        filters.append(query.Term(
            assigned_curator=cleaned_data['assigned_curator']))
    if cleaned_data.get('flagged'):
        filters.append(query.Term(
            flagged=cleaned_data['flagged'] == 'True'))
    if cleaned_data.get('is_archived'):
        filters.append(query.Term(
            is_archived=cleaned_data['is_archived'] == 'True'))
    if cleaned_data.get('contact_email'):
        filters.append(query.Term(has_contact_email=True))

    if must or filters:
        search = search.query(query.Bool(must=must, filter=filters))
    if not text:
        search = search.sort('-date_published', '-last_modified')
    return search


def curator_status_facets(search):
    get_es_client()
    facet_search = search[:0]
    facet_search.aggs.bucket('status', aggs.Terms(field='status', size=20))
    response = facet_search.execute()
    return [(bucket.key, bucket.doc_count) for bucket in response.aggs.status.buckets]


def autocomplete_documents(model, text, size=25):
    doc_class = get_search_index(model)
    search = doc_class.search()
    if text:
        search = search.query('match', name={'query': text, 'operator': 'and'})
    return search[:size].execute()


autocomplete_analyzer = analyzer('autocomplete_analyzer',
                                 tokenizer=tokenizer(
                                    'edge_ngram_tokenizer',
                                    type='edge_ngram',
                                    min_gram=3,
                                    max_gram=10,
                                    token_chars=[
                                        "letter",
                                        "digit"
                                    ]),
                                 filter=['lowercase', 'asciifolding', 'trim'])


def get_search_index(model):
    # ensure the dsl default connection is configured so that
    # ``Document.search().execute()`` resolves it (no network I/O)
    get_es_client()
    lookup = {
        Author: AuthorDoc,
        Container: ContainerDoc,
        ModelDocumentation: ModelDocumentationDoc,
        Platform: PlatformDoc,
        Sponsor: SponsorDoc,
        Tag: TagDoc,
    }
    try:
        return lookup[model]
    except KeyError:
        raise ValidationError(_('Invalid model_name'), code='invalid')


class AuthorDoc(Document):
    id = edsl.Integer(required=True)
    orcid = edsl.Keyword()
    researcherid = edsl.Keyword()
    name = edsl.Text(copy_to=ALL_DATA_FIELD,
                     analyzer=autocomplete_analyzer,
                     search_analyzer='standard')

    @classmethod
    def from_instance(cls, author):
        doc = cls(meta = {'id': author.id},
                  id = author.id,
                  orcid = author.orcid,
                  researcherid = author.researcherid,
                  name = author.name)
        return doc.to_dict(include_meta=True)

    class Index:
        name = 'author'
        settings = {
            'number_of_shards': 1,
            # single-node cluster: replicas only add write overhead
            'number_of_replicas': 0
        }


class ContainerDoc(Document):
    id = edsl.Integer(required=True)
    name = edsl.Text(copy_to=ALL_DATA_FIELD,
                     analyzer=autocomplete_analyzer,
                     search_analyzer='standard')
    issn = edsl.Keyword()

    @classmethod
    def from_instance(cls, container):
        doc = cls(meta = {'id': container.id},
                  id = container.id,
                  name = container.name,
                  issn = container.issn)
        return doc.to_dict(include_meta=True)

    class Index:
        name = 'container'
        settings = {
            'number_of_shards': 1,
            # single-node cluster: replicas only add write overhead
            'number_of_replicas': 0
        }


class PlatformDoc(Document):
    id = edsl.Integer(required=True)
    name = edsl.Text(copy_to=ALL_DATA_FIELD,
                     analyzer=autocomplete_analyzer,
                     search_analyzer='standard')

    @classmethod
    def from_instance(cls, instance):
        doc = cls(meta = {'id': instance.id},
                  id = instance.id,
                  name = instance.name)
        return doc.to_dict(include_meta=True)

    class Index:
        name = 'platform'
        settings = {
            'number_of_shards': 1,
            # single-node cluster: replicas only add write overhead
            'number_of_replicas': 0
        }


class SponsorDoc(Document):
    id = edsl.Integer(required=True)
    name = edsl.Text(copy_to=ALL_DATA_FIELD,
                     analyzer=autocomplete_analyzer,
                     search_analyzer='standard')

    @classmethod
    def from_instance(cls, instance):
        doc = cls(meta = {'id': instance.id},
                  id = instance.id,
                  name = instance.name)
        return doc.to_dict(include_meta=True)

    class Index:
        name = 'sponsor'
        settings = {
            'number_of_shards': 1,
            # single-node cluster: replicas only add write overhead
            'number_of_replicas': 0
        }


class TagDoc(Document):
    id = edsl.Integer(required=True)
    name = edsl.Text(copy_to=ALL_DATA_FIELD)

    @classmethod
    def from_instance(cls, instance):
        doc = cls(meta={'id': instance.id},
                  id = instance.id,
                  name = instance.name)
        return doc.to_dict(include_meta=True)

    class Index:
        name = 'tag'
        settings = {
            'number_of_shards': 1,
            # single-node cluster: replicas only add write overhead
            'number_of_replicas': 0
        }


class ModelDocumentationDoc(Document):
    id = edsl.Integer(required=True)
    name = edsl.Text(
        copy_to=ALL_DATA_FIELD,
        analyzer=autocomplete_analyzer,
        search_analyzer='standard',
    )

    @classmethod
    def from_instance(cls, instance):
        doc = cls(meta={'id': instance.id}, id=instance.id, name=instance.name)
        return doc.to_dict(include_meta=True)

    class Index:
        name = 'model_documentation'
        settings = INDEX_SETTINGS


RELATED_DOCUMENTS = {
    Author: AuthorDoc,
    Container: ContainerDoc,
    ModelDocumentation: ModelDocumentationDoc,
    Platform: PlatformDoc,
    Sponsor: SponsorDoc,
    Tag: TagDoc,
}


def _document_actions(doc_class, queryset):
    for instance in queryset.iterator(chunk_size=500):
        yield doc_class.from_instance(instance)


def _curator_contributor_data():
    publication_table = Publication._meta.model_name
    rows = list(
        AuditLog.objects.filter(
            Q(table=publication_table) | Q(pub_id__isnull=False),
            audit_command__action='MANUAL',
        )
        .annotate(
            search_publication_id=Case(
                When(table=publication_table, then=F('row_id')),
                default=F('pub_id'),
                output_field=BigIntegerField(),
            )
        )
        .values('search_publication_id', 'audit_command__creator__username')
        .annotate(
            contribution_count=Count('id'),
            last_contribution=Max('audit_command__date_added'),
        )
    )
    totals = defaultdict(int)
    for row in rows:
        totals[row['search_publication_id']] += row['contribution_count']

    contributions = defaultdict(list)
    for row in rows:
        publication_id = row['search_publication_id']
        percentage = row['contribution_count'] * 100 // totals[publication_id]
        value = '{0} ({1})%'.format(
            row['audit_command__creator__username'],
            percentage,
        )
        contributions[publication_id].append((row['last_contribution'], value))

    return {
        publication_id: [value for _, value in sorted(values, reverse=True)]
        for publication_id, values in contributions.items()
    }


def _publication_documents(queryset, doc_class):
    queryset = queryset.select_related('container', 'assigned_curator').prefetch_related(
        'code_archive_urls',
        'tags',
        'sponsors',
        'platforms',
        'creators',
        'model_documentation',
    )
    if doc_class is not CuratorPublicationDoc:
        yield from _document_actions(doc_class, queryset)
        return

    contributor_data = _curator_contributor_data()
    for publication in queryset.iterator(chunk_size=500):
        yield doc_class.from_instance(
            publication,
            contributor_data=contributor_data.get(publication.pk, []),
        )


def search_index_counts():
    return {
        AuthorDoc._index._name: Author.objects.count(),
        ContainerDoc._index._name: Container.objects.count(),
        ModelDocumentationDoc._index._name: ModelDocumentation.objects.count(),
        PlatformDoc._index._name: Platform.objects.count(),
        SponsorDoc._index._name: Sponsor.objects.count(),
        TagDoc._index._name: Tag.objects.count(),
        PublicationDoc._index._name: Publication.api.primary().filter(
            status=Publication.Status.REVIEWED).count(),
        CuratorPublicationDoc._index._name: Publication.api.primary().count(),
    }


def bulk_index_all():
    client = get_es_client()
    related_querysets = (
        (AuthorDoc, Author.objects.all()),
        (ContainerDoc, Container.objects.all()),
        (ModelDocumentationDoc, ModelDocumentation.objects.all()),
        (PlatformDoc, Platform.objects.all()),
        (SponsorDoc, Sponsor.objects.all()),
        (TagDoc, Tag.objects.all()),
    )
    builds = [
        (doc_class, _document_actions(doc_class, queryset), queryset.count())
        for doc_class, queryset in related_querysets
    ]

    public_publications = Publication.api.primary().filter(
        status=Publication.Status.REVIEWED)
    curator_publications = Publication.api.primary()
    builds.extend((
        (
            PublicationDoc,
            _publication_documents(public_publications, PublicationDoc),
            public_publications.count(),
        ),
        (
            CuratorPublicationDoc,
            _publication_documents(curator_publications, CuratorPublicationDoc),
            curator_publications.count(),
        ),
    ))
    return rebuild_document_indices(client, builds)


def bulk_index_public():
    return bulk_index_all()


def validate_search_indices():
    client = get_es_client()
    expected_counts = search_index_counts()
    actual_counts = {}
    for alias, expected_count in expected_counts.items():
        if _alias_target(client, alias) is None:
            raise SearchRebuildError("search alias '{0}' is missing".format(alias))
        actual_count = _index_doc_count(client, alias)
        actual_counts[alias] = actual_count
        if actual_count != expected_count:
            raise SearchRebuildError(
                "alias '{0}' has {1} documents; expected {2}".format(
                    alias, actual_count, expected_count))
    client.search(index=PublicationDoc._index._name, size=1, query={'match_all': {}})
    client.search(index=CuratorPublicationDoc._index._name, size=1, query={'match_all': {}})
    return actual_counts


def _delete_document(client, doc_class, pk):
    client.options(ignore_status=[404]).delete(
        index=doc_class._index._name,
        id=pk,
        refresh='wait_for',
    )


def _index_document(client, doc_class, instance):
    action = doc_class.from_instance(instance)
    client.index(
        index=doc_class._index._name,
        id=instance.pk,
        document=action['_source'],
        refresh='wait_for',
    )


def refresh_search_documents(publication_ids=(), sender=None, related_ids=()):
    client = get_es_client()
    requested_publication_ids = {int(pk) for pk in publication_ids}
    publications = Publication.api.primary().filter(
        pk__in=requested_publication_ids,
    ).select_related('container', 'assigned_curator').prefetch_related(
        'code_archive_urls',
        'tags',
        'sponsors',
        'platforms',
        'creators',
        'model_documentation',
    )
    publications_by_id = {publication.pk: publication for publication in publications}

    for publication_id in requested_publication_ids:
        publication = publications_by_id.get(publication_id)
        if publication is None:
            _delete_document(client, PublicationDoc, publication_id)
            _delete_document(client, CuratorPublicationDoc, publication_id)
            continue
        _index_document(client, CuratorPublicationDoc, publication)
        if publication.status == Publication.Status.REVIEWED:
            _index_document(client, PublicationDoc, publication)
        else:
            _delete_document(client, PublicationDoc, publication_id)

    doc_class = RELATED_DOCUMENTS.get(sender)
    if doc_class is None:
        return
    requested_related_ids = {int(pk) for pk in related_ids}
    instances = sender.objects.filter(pk__in=requested_related_ids)
    instances_by_id = {instance.pk: instance for instance in instances}
    for related_id in requested_related_ids:
        instance = instances_by_id.get(related_id)
        if instance is None:
            _delete_document(client, doc_class, related_id)
        else:
            _index_document(client, doc_class, instance)
