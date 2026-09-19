import pandas as pd
from django.test import SimpleTestCase

from catalog.core.visualization.plots import archival_timeseries_plot


class ArchivalTimeseriesPlotTest(SimpleTestCase):
    def test_combines_archive_categories_with_publication_totals(self):
        publication_df = pd.DataFrame.from_records([
            {'id': 1, 'year_published': 2020},
            {'id': 2, 'year_published': 2020},
            {'id': 3, 'year_published': 2021},
        ]).set_index('id')
        archive_df = pd.DataFrame.from_records([
            {'publication_id': 1, 'category': 'Archive'},
            {'publication_id': 2, 'category': 'Repository'},
        ]).set_index('publication_id')

        result = archival_timeseries_plot(publication_df, archive_df, [1, 2])

        count_traces = {trace.name: trace for trace in result['count'].data}
        year_2020 = list(count_traces['Total'].x).index(2020)
        self.assertEqual(count_traces['Archive'].y[year_2020], 1)
        self.assertEqual(count_traces['Repository'].y[year_2020], 1)
        self.assertEqual(count_traces['Total'].y[year_2020], 2)
