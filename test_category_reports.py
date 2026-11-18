"""Category report contracts against a real disk-backed ThermalStore."""

import csv
import json
import math
import tempfile
import unittest
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import category_reports as reports
from large_data_store import ThermalStore


class ReportParser(HTMLParser):
    def __init__(self, path):
        super().__init__()
        self.tables = {}
        self.figures = []
        self.links = []
        self.heading = ''
        self.capture = None
        self.parts = []
        self.headers = []
        self.row = []
        self.external_scripts = []
        self.test_controls = []
        self.test_sections = []
        self.socket_groups = []
        self.table_sections = []
        self.test_selector = None
        self.feed(path.read_text(encoding='utf-8'))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('class') == 'test-enabled':
            self.test_controls.append(attrs)
        if tag == 'section' and attrs.get('class') == 'test-result':
            self.test_sections.append(attrs)
        if tag == 'details' and attrs.get('class') == 'socket-result':
            self.socket_groups.append(attrs)
        if tag == 'details' and attrs.get('class') == 'table-section':
            self.table_sections.append(attrs)
        if tag == 'fieldset' and attrs.get('class') == 'test-selection':
            self.test_selector = attrs
        if tag == 'script' and 'src' in attrs:
            self.external_scripts.append(attrs['src'])
        if tag == 'a':
            self.links.append(attrs['href'])
        if tag == 'table':
            self.tables[self.heading] = []
            self.headers = []
        if tag == 'tr':
            self.row = []
        if tag in ('h2', 'th', 'td') or (tag == 'script' and attrs.get('class') == 'plot-data'):
            self.capture = tag
            self.parts = []

    def handle_data(self, data):
        if self.capture:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == self.capture:
            text = ''.join(self.parts)
            if tag == 'h2':
                self.heading = text
            elif tag == 'th':
                self.headers.append(text)
            elif tag == 'td':
                self.row.append(text)
            elif tag == 'script':
                self.figures.append(json.loads(text))
            self.capture = None
        if tag == 'tr' and self.row:
            self.tables[self.heading].append(dict(zip(self.headers, self.row)))


class CategoryReportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = ThermalStore(self.root / 'cache.duckdb', memory_limit='128MB')
        self.addCleanup(self.store.close)
        # Most tests need the report contract, not repeated 5 MB library parsing.
        self.library = patch.object(reports, 'get_plotlyjs', return_value='/* offline Plotly */')
        self.library.start()
        self.addCleanup(self.library.stop)

    def add(self, value, unit='u1', test='IPC::ARR_ATOM::DTSMON_100', lot='L1',
            sensor='sensor_a', source='source_a', product='P1', socket=None):
        self.store.connection.execute(
            'INSERT INTO readings (source, Test, Measurement_Point, Value, Unit_ID, Lot, Product_Name, Socket) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)', [source, test, sensor, value, unit, lot, product, socket])

    def export(self, kind='dtsmon', **kwargs):
        options = dict(filters={}, sensor_config={}, limits={}, source_note='fixture', plot_limit=2)
        if kind == 'dts':
            options['sensor_config'] = None
        options.update(kwargs)
        path = getattr(reports, 'export_' + kind)(self.store, self.root / kind, **options)
        self.assertEqual(path.name, 'dashboard.html')
        self.assert_no_temp_tables()
        return path, ReportParser(path)

    def assert_no_temp_tables(self):
        names = self.store.connection.execute(
            "SELECT table_name FROM duckdb_tables() WHERE temporary").fetchall()
        self.assertEqual(names, [])

    def test_diode_selected_product_combines_lots_and_raw_products_per_socket(self):
        import re

        for lot, product, value in [('L1', 'raw-A', 90), ('L2', 'raw-B', 100), ('L3', 'raw-A', 110)]:
            self.add(value, test='PCS_SOT0', lot=lot, product=product, unit=lot, socket='CLASSHOT', source=lot)
            self.add(value - 100, test='PCS_SOT0', lot=lot, product=product, unit=lot, socket='CSM', source=lot)
        limits = {'NVL_Class_S8C': [{'test': 'PCS_SOT0', 'sockets': {
            'CLASSHOT': {'LCL': 93, 'UCL': 107}, 'CSM': {'LCL': -15, 'UCL': 5}}}]}
        path, parsed = self.export('diode', per_socket=True, report_product='S8C',
                                   limit_product='NVL_Class_S8C', limits=limits, plot_limit=20)
        self.assertEqual(len(parsed.figures), 2)
        for figure in parsed.figures:
            metadata = figure['layout']['meta']
            self.assertEqual(metadata['count'], 3)
            self.assertEqual(metadata['units'], 3)
            self.assertEqual(metadata['report_product'], 'S8C')
            expected = (93, 107) if metadata['socket'] == 'CLASSHOT' else (-15, 5)
            self.assertEqual((metadata['lcl'], metadata['ucl']), expected)
        rows = parsed.tables['Full-data product / test statistics']
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['Product_Name'] for row in rows}, {'S8C'})
        scatter = [json.loads(value) for value in re.findall(
            r'<script class="plot-option" data-chart-type="scatter"[^>]*>(.*?)</script>',
            path.read_text(encoding='utf-8'), re.S)]
        self.assertEqual(len(scatter), 2)
        for figure in scatter:
            self.assertEqual({point[0] for point in figure['data'][0]['customdata']}, {'L1', 'L2', 'L3'})
        _, combined = self.export('diode', report_product='S8C', limits=limits,
                                  filters={'Lot': ['L1', 'L2']}, plot_limit=20)
        self.assertEqual(len(combined.figures), 1)
        self.assertEqual(combined.figures[0]['layout']['meta']['count'], 4)

    def test_dtsmon_hardware_groups_preserve_full_names_and_maxima(self):
        for scope in ('IPC', 'IPH', 'IPG'):
            for kind in ('ARR', 'FUN'):
                self.add(95, test=f'{scope}::{kind}_MODULE::TEMP_DTS')
                self.add(105, test=f'{scope}::{kind}_MODULE::TEMP_DTS', sensor='sensor_b')
        path, parsed = self.export(group_by_hardware=True, plot_limit=20)
        self.assertEqual({figure['layout']['title']['text'] for figure in parsed.figures}, {'CPU', 'HUB', 'GT'})
        self.assertEqual(sum(len(figure['data'][0]['y']) for figure in parsed.figures), 6)
        self.assertTrue(all(value == 105 for figure in parsed.figures for value in figure['data'][0]['y']))
        self.assertIn('CPU / HUB / GT summary', path.read_text(encoding='utf-8'))

    def histogram(self, figure):
        return next(trace for trace in figure['data'] if trace['type'] == 'bar')

    def test_dts_variability_uses_reference_domains_and_full_data_means(self):
        for value in range(10):
            self.add(value, test='DTS_SAMPLE', sensor='CCF0S0', socket='CLASSHOT')
        self.add(20, test='DTS_SAMPLE', sensor='AON', socket='CLASSHOT')
        self.add(100, test='DTS_SAMPLE', sensor='AON', socket='CSM')
        path, parsed = self.export('dts', per_socket=True, plot_limit=2,
                                  limits={'P1': [{'test': 'DTS_SAMPLE', 'LCL': 1, 'UCL': 8}]})
        self.assertEqual(len(parsed.figures), 2)
        hot = parsed.figures[0]
        self.assertEqual(hot['layout']['meta']['total_readings'], 11)
        self.assertEqual(hot['layout']['meta']['sampled_readings'], 2)
        self.assertEqual(set(hot['data'][0]['x']), {'SOC', 'CCF0'})
        self.assertEqual({shape['y0'] for shape in hot['layout']['shapes']}, {1, 8})
        self.assertEqual(len(parsed.socket_groups), 2)
        content = path.read_text(encoding='utf-8')
        self.assertIn('By domain', content)
        self.assertIn('By sensor', content)
        self.assertIn('Full-data sensor mean', content)
        self.assertIn('4.5', content)
        self.assertEqual(reports._dts_domain('C7S18'), 'C7')
        self.assertEqual(reports._dts_domain('A3S29'), 'A3')
        self.assertEqual(reports._dts_domain('CCF1S4'), 'Unknown')
        self.assertEqual(reports._dts_domain('unmapped'), 'Unknown')

    def test_dts_export_requires_exact_configured_test_names(self):
        self.add(80, test='PTH_DTS_MODULE::LISTED', sensor='AON')
        self.add(100, test='PTH_DTS_MODULE::LISTED_SUFFIX', sensor='AON')
        self.add(120, test='PTH_DTS_OTHER::LISTED', sensor='AON')
        _, parsed = self.export('dts', sensor_config={
            'PTH_DTS_MODULE::LISTED': {'COLUMN_NAMES': ['AON'], 'CATEGORIES': ['dts']}})
        self.assertEqual(len(parsed.figures), 1)
        self.assertEqual(parsed.figures[0]['data'][0]['y'], [80])
        _, empty = self.export('dts', sensor_config={})
        self.assertEqual(empty.figures, [])

    def test_dts_sigma_limits_use_full_data_per_sensor_and_socket(self):
        for value in (10, 20, 30):
            self.add(value, test='DTS_SAMPLE', sensor='AON', socket='CLASSHOT')
        self.add(100, test='DTS_SAMPLE', sensor='AON', socket='CSM')
        path, parsed = self.export('dts', per_socket=True, plot_limit=1)
        hot, single = parsed.figures
        self.assertEqual(hot['layout']['meta']['sigma_statistics'],
                         [{'sensor': 'AON', 'count': 3, 'mean': 20.0, 'stddev': 10.0}])
        limits = [trace for trace in hot['data'] if trace.get('meta', {}).get('sigma_bound')]
        self.assertEqual([trace['y'] for trace in limits], [[-10.0], [50.0]])
        self.assertTrue(all(trace['y'] == [None] for trace in single['data'] if trace.get('meta', {}).get('sigma_bound')))
        rows = parsed.tables['Full-data sensor statistics']
        self.assertEqual(float(rows[0]['Sigma_LCL']), -10)
        self.assertEqual(float(rows[0]['Sigma_UCL']), 50)
        content = path.read_text(encoding='utf-8')
        self.assertIn('id="sigma-multiplier"', content)
        self.assertIn('dts-calculated-limits.csv', content)

    def test_diode_backtrace_keeps_point_and_full_data_equipment_identity(self):
        for index in range(5):
            self.add(90 + index, unit=f'u{index}', test='DIODE', socket='PHMCOLD')
        self.store.connection.execute("UPDATE readings SET Unit_Tester_Id='tester-01', Thermal_Head_Id='head-02', Unit_Tester_Site_Id='site-3'")
        path, parsed = self.export('diode', plot_limit=2, per_socket=True)
        rows = parsed.tables['Full-data equipment statistics']
        self.assertEqual(rows[0]['readings'], '5')
        self.assertEqual(rows[0]['Unit_Tester_Id'], 'tester-01')
        self.assertEqual(rows[0]['Thermal_Head_Id'], 'head-02')
        import re
        content = path.read_text(encoding='utf-8')
        scatter = json.loads(re.search(r'<script class="plot-option" data-chart-type="scatter" type="application/json">(.*?)</script>', content, re.S)[1])
        self.assertEqual(len(scatter['data'][0]['customdata']), 2)
        self.assertEqual(scatter['data'][0]['customdata'][0][3:7], ['tester-01', 'head-02', 'PHMCOLD', 'site-3'])
        trace = next(path.parent.glob('diode-trace-*.csv'))
        with trace.open(encoding='utf-8', newline='') as stream:
            readings = list(csv.DictReader(stream))
        self.assertEqual(len(readings), 5)
        self.assertEqual({row['Unit_ID'] for row in readings}, {f'u{index}' for index in range(5)})

    def test_dts_has_one_summary_after_all_test_and_socket_plots(self):
        self.add(80, test='PTH_DTS_MODULE::FIRST', sensor='AON', socket='CLASSHOT')
        self.add(100, test='PTH_DTS_MODULE::SECOND', sensor='C0S0', socket='CLASSHOT')
        self.add(120, test='PTH_DTS_MODULE::FIRST', sensor='AON', socket='CSM')
        path, parsed = self.export('dts', per_socket=True)
        content = path.read_text(encoding='utf-8')
        self.assertEqual(len(parsed.figures), 3)
        self.assertEqual(content.count('<table'), 1)
        self.assertGreater(content.index('<table'), content.rindex('class="plot-data"'))
        self.assertGreater(content.index('<table'), content.index('<section class="dts-statistics">'))
        rows = parsed.tables['Full-data sensor statistics']
        self.assertEqual(len(rows), 3)
        self.assertEqual({(row['Test'], row['Socket'], row['Sensor']) for row in rows}, {
            ('PTH_DTS_MODULE::FIRST', 'CLASSHOT', 'AON'),
            ('PTH_DTS_MODULE::SECOND', 'CLASSHOT', 'C0S0'),
            ('PTH_DTS_MODULE::FIRST', 'CSM', 'AON')})

    def test_dts_chart_views_keep_equipment_metadata(self):
        import re

        self.add(88, unit='unit-1', test='PTH_DTS_MODULE::FIRST', sensor='AON', socket='CLASSHOT')
        self.store.connection.execute("UPDATE readings SET Thermal_Head_Id='head-1', Unit_Tester_Id='tester-1', Unit_Tester_Site_Id='A102'")
        path, parsed = self.export('dts', per_socket=True)
        content = path.read_text(encoding='utf-8')
        scatter = json.loads(re.search(r'<script class="plot-option" data-chart-type="scatter" type="application/json">(.*?)</script>', content, re.S)[1])
        for figure in [*parsed.figures, scatter]:
            point = figure['data'][0]['customdata'][0]
            self.assertEqual(point[1], 'unit-1')
            self.assertEqual(point[3:], ['CLASSHOT', 'head-1', 'tester-1', 'A102'])
            self.assertIn('Tester site: %{customdata[6]}', figure['data'][0]['hovertemplate'])

    def test_all_category_tables_start_collapsed(self):
        self.add(88, test='DTS_SAMPLE', sensor='AON', socket='CLASSHOT')
        for kind in ('diode', 'dts', 'dtsmon'):
            with self.subTest(kind=kind):
                _, parsed = self.export(kind, per_socket=True)
                self.assertTrue(parsed.table_sections)
                self.assertEqual(len(parsed.table_sections), len(parsed.tables))
                self.assertTrue(all('open' not in attrs for attrs in parsed.table_sections))

    def test_dts_sigma_pools_all_readings_within_test_and_socket(self):
        import statistics

        for value, sensor in [(10, 'AON'), (20, 'AON'), (30, 'AON'), (110, 'CCF0S0')]:
            self.add(value, test='DTS_SAMPLE', sensor=sensor, socket='CLASSHOT')
        self.add(900, test='OTHER_TEST', sensor='AON', socket='CLASSHOT')
        self.add(700, test='DTS_SAMPLE', sensor='AON', socket='CSM')
        _, parsed = self.export('dts', per_socket=True, plot_limit=1)
        expected_sd = statistics.stdev([10, 20, 30, 110])
        pooled = next(figure for figure in parsed.figures if 'DTS_SAMPLE' in figure['layout']['meta']['export_title']
                      and figure['layout']['meta']['socket'] == 'CLASSHOT')
        for row in pooled['layout']['meta']['sigma_statistics']:
            self.assertEqual(row['count'], 4)
            self.assertEqual(row['mean'], 42.5)
            self.assertAlmostEqual(row['stddev'], expected_sd)
        for trace in pooled['data']:
            bound = trace.get('meta', {}).get('sigma_bound')
            if bound:
                expected = 42.5 + (-3 if bound == 'lower' else 3) * expected_sd
                self.assertEqual(len(trace['y']), 2)
                for value in trace['y']:
                    self.assertAlmostEqual(value, expected)
        rows = [row for row in parsed.tables['Full-data sensor statistics']
                if row['Test'] == 'DTS_SAMPLE' and row['Socket'] == 'CLASSHOT']
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(float(row['Test_mean']), 42.5)
            self.assertAlmostEqual(float(row['Test_stddev_samp']), expected_sd)
            self.assertAlmostEqual(float(row['Sigma_LCL']), 42.5 - 3 * expected_sd)
            self.assertAlmostEqual(float(row['Sigma_UCL']), 42.5 + 3 * expected_sd)

    def test_socket_metadata_ingests_process_steps_and_explicit_sockets(self):
        folder = self.root / 'input'
        folder.mkdir()
        with (folder / 'sockets.csv').open('w', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(['TEST_NAME', 'TEST_RESULT', 'VISUAL_ID', 'Process Step', 'Socket',
                             'PCS_STIM_RECIPE_VERSION'])
            writer.writerows([
                ['DIODE', '100', 'u1', 'CLASSHOT', '', 'R1'],
                ['DIODE', '80', 'u1', 'CSM', '', 'R2'],
                ['DIODE', '90', 'u1', 'PHMHOT', '', 'R3'],
                ['DIODE', '95', 'u1', 'CLASSHOT', ' OTHER ', 'R4'],
                ['DIODE', '99', 'u2', '', '', 'R5'],
            ])
        self.store.batch_size = 1
        self.store.refresh(folder, {'DIODE': {'COLUMN_NAMES': ['CPU0']}}, {})
        self.assertEqual(self.store.connection.execute(
            'SELECT Socket, Value FROM readings ORDER BY Socket').fetchall(),
            [('', 99), ('CLASSHOT', 100), ('CSM', 80), ('OTHER', 95), ('PHMHOT', 90)])
        self.assertEqual(self.store.connection.execute(
            'SELECT Socket, Recipe_Revision FROM recipe_revisions ORDER BY Socket').fetchall(),
            [('', 'R5'), ('CLASSHOT', 'R1'), ('CSM', 'R2'), ('OTHER', 'R4'), ('PHMHOT', 'R3')])
        self.store.refresh(folder, {'DIODE': {'COLUMN_NAMES': ['CPU0']}}, {})
        self.assertEqual(self.store.overview({'Socket': ['CSM']})['readings'], 1)

    def test_equipment_and_socket_aliases_survive_ingestion(self):
        folder = self.root / 'equipment-input'
        folder.mkdir()
        (folder / 'readings.csv').write_text(
            'TEST_NAME,TEST_RESULT,VISUAL_ID,TesterId,Thermal_Head_Id,Unit_Tester_Site_Id,SocketName,ProcessStep\n'
            'DIODE,97,unit,tester-1,head-2,site-3,N/A,phm room\n', encoding='utf-8')
        self.store.refresh(folder, {'DIODE': {'COLUMN_NAMES': ['CPU0']}}, {})
        self.assertEqual(self.store.connection.execute(
            'SELECT Unit_Tester_Id, Thermal_Head_Id, Unit_Tester_Site_Id, Socket FROM readings').fetchall(),
            [('tester-1', 'head-2', 'site-3', 'PHMROOM')])

    def test_recipe_metadata_is_preserved_without_numeric_readings(self):
        folder = self.root / 'input'
        folder.mkdir()
        source = folder / 'recipes.csv'
        with source.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(['TEST_NAME', 'TEST_RESULT', 'VISUAL_ID', 'Lot', 'Product_Name', 'Program Name'])
            writer.writerows([
                ['DIODE', '100', 'u1', 'L1', 'P1', 'PGM'],
                ['PCS_STIM_RECIPE_VERSION', '001.02', 'u1', 'L1', 'P1', 'PGM'],
                ['RECIPEMANIFEST', '{"recipe":"R1|R2","rev":"<3>"}', 'u1', 'L1', 'P1', 'PGM'],
            ])
        self.store.refresh(folder, {'DIODE': {'COLUMN_NAMES': ['CPU0']}}, {})
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM readings').fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute(
            'SELECT Recipe_Field, Recipe_Revision FROM recipe_revisions ORDER BY Recipe_Field').fetchall(),
            [('PCS_STIM_RECIPE_VERSION', '001.02'), ('RECIPEMANIFEST', '{"recipe":"R1|R2","rev":"<3>"}')])
        self.store.refresh(folder, {'DIODE': {'COLUMN_NAMES': ['CPU0']}}, {})
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM recipe_revisions').fetchone()[0], 2)
        source.unlink()
        self.store.refresh(folder, {}, {})
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM recipe_revisions').fetchone()[0], 0)

    def test_socket_cache_migration_preserves_rows_and_reimports_metadata(self):
        folder = self.root / 'migration-input'
        folder.mkdir()
        with (folder / 'data.csv').open('w', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(['TEST_NAME', 'TEST_RESULT', 'VISUAL_ID', 'ProcessStep', 'PCS_STIM_RECIPE_VERSION'])
            writer.writerow(['DIODE', '100', 'u1', 'CSM', 'R1'])
        with patch('large_data_store.CACHE_VERSION', 6):
            self.store.refresh(folder, {}, {})
        self.store.connection.execute('ALTER TABLE readings DROP COLUMN Socket')
        self.store.connection.execute('ALTER TABLE recipe_revisions DROP COLUMN Socket')
        self.store.close()
        self.store = ThermalStore(self.root / 'cache.duckdb', memory_limit='128MB')
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.connection.execute('SELECT Value, Socket FROM readings').fetchall(), [(100, '')])
        self.assertFalse(self.store.is_current(folder, {}, {}))
        self.store.refresh(folder, {}, {})
        self.assertEqual(self.store.connection.execute('SELECT Value, Socket FROM readings').fetchall(), [(100, 'CSM')])
        self.assertEqual(self.store.connection.execute(
            'SELECT Recipe_Revision, Socket FROM recipe_revisions').fetchall(), [('R1', 'CSM')])

    def test_socket_recipe_selection_does_not_leak_across_steps(self):
        for socket, revision in [('CLASSHOT', 'R1'), ('CSM', 'R2'), ('PHMHOT', 'R3')]:
            self.add(100, test='DIODE', socket=socket)
            self.store.connection.execute(
                'INSERT INTO recipe_revisions '
                '(source, Unit_ID, Lot, Program_Name, Product_Name, Recipe_Field, Recipe_Revision, Socket) '
                'VALUES (?, ?, ?, NULL, ?, ?, ?, ?)',
                ['source_a', 'u1', 'L1', 'P1', 'PCS_STIM_RECIPE_VERSION', revision, socket])
        _, parsed = self.export('diode', per_socket=True, filters={'Socket': ['CSM']})
        recipes = parsed.tables['Recipe Revision']
        self.assertEqual([(row['Socket'], row['Recipe_Revision'], row['Units']) for row in recipes],
                         [('CSM', 'R2', '1')])

    def test_recipe_wide_columns_are_bounded_and_failed_import_removes_metadata(self):
        folder = self.root / 'input'
        folder.mkdir()
        with (folder / 'wide.csv').open('w', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(['TEST_NAME', 'TEST_RESULT', 'VISUAL_ID', 'Lot', 'PCS_STIM_RECIPE_VERSION'])
            writer.writerows([['DIODE', '100', 'u1', 'L1', '0007'], ['DIODE', '101', 'u2', 'L1', '0008']])
        self.store.batch_size = 1
        with patch.object(self.store, '_insert_recipes', wraps=self.store._insert_recipes) as insert:
            self.store.refresh(folder, {}, {})
            self.assertEqual(insert.call_count, 2)
            self.assertTrue(all(len(call.args[0]) == 1 for call in insert.call_args_list))
        self.assertEqual(self.store.connection.execute(
            'SELECT Recipe_Revision FROM recipe_revisions ORDER BY Recipe_Revision').fetchall(), [('0007',), ('0008',)])
        self.store.invalidate()
        with patch.object(self.store, '_insert', side_effect=RuntimeError('failed batch')):
            with self.assertRaisesRegex(RuntimeError, 'failed batch'):
                self.store.refresh(folder, {}, {})
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM recipe_revisions').fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM readings').fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM sources').fetchone()[0], 0)

    def test_recipe_table_follows_statistics_and_respects_selected_units(self):
        for unit in ('u1', 'u2'):
            self.add(100, unit=unit, test='DIODE')
        self.add(90, unit='other', test='OTHER')
        for unit, revision, source, lot in (
                ('u1', '001.02<&>', 'source_a', 'L1'), ('u1', '001.02<&>', 'source_a', 'L1'),
                ('u2', '002', 'source_a', 'L1'), ('other', 'WRONG_TEST', 'source_a', 'L1'),
                ('u1', 'WRONG_SOURCE', 'source_b', 'L1'), ('u1', 'WRONG_LOT', 'source_a', 'L2')):
            self.store.connection.execute('INSERT INTO recipe_revisions '
                '(source, Unit_ID, Lot, Program_Name, Product_Name, Recipe_Field, Recipe_Revision) '
                'VALUES (?, ?, ?, NULL, ?, ?, ?)',
                [source, unit, lot, 'P1', 'PCS_STIM_RECIPE_VERSION', revision])
        path, parsed = self.export('diode', filters={'Test': ['DIODE']}, report_product='Assigned')
        recipes = parsed.tables['Recipe Revision']
        self.assertEqual([row['Recipe_Revision'] for row in recipes], ['001.02<&>', '002'])
        self.assertEqual([row['Units'] for row in recipes], ['1', '1'])
        self.assertEqual({row['Product_Name'] for row in recipes}, {'Assigned'})
        content = path.read_text(encoding='utf-8')
        self.assertIn('001.02&lt;&amp;&gt;', content)
        self.assertLess(content.index('<h2>Full-data product / test statistics'), content.index('<h2>Recipe Revision'))
        self.assertEqual(parsed.tables['Full-data product / test statistics'][0]['count'], '2')
        _, empty = self.export('diode', filters={'Test': []})
        self.assertEqual(empty.tables['Recipe Revision'], [])

    def test_diode_chart_precedes_summary_and_has_render_feedback(self):
        self.add(100, test='DIODE')
        path, parsed = self.export('diode')
        content = path.read_text(encoding='utf-8')
        self.assertEqual(len(parsed.figures), 1)
        self.assertLess(content.index('class="plot-panel"'), content.index('Full-data product / test statistics'))
        self.assertIn('if (charts.length) renderChart(charts[0])', content)
        self.assertIn("typeof IntersectionObserver === 'function'", content)
        self.assertIn('Chart could not be displayed:', content)
        self.assertIn('<noscript>', content)

    def test_diode_socket_groups_keep_statistics_samples_and_limits_separate(self):
        for socket, values in [('CLASSHOT', [100, 102]), ('CSM', [80]), ('PHMHOT', [90]),
                               ('OTHER<&>', [110]), (None, [50]), ('', [52])]:
            for value in values:
                self.add(value, test='DIODE', socket=socket)
        with patch.object(reports, '_BATCH', 1):
            path, parsed = self.export('diode', per_socket=True, plot_limit=1,
                                      limits={'P1': [{'test': 'DIODE', 'LCL': 95, 'UCL': 105}]})
        rows = {row['Socket']: row for row in parsed.tables['Full-data product / test statistics']}
        self.assertEqual(set(rows), {'CLASSHOT', 'CSM', 'PHMHOT', 'OTHER<&>', 'Unknown socket'})
        self.assertEqual(rows['CLASSHOT']['count'], '2')
        self.assertEqual(float(rows['CLASSHOT']['mean']), 101)
        self.assertEqual(rows['CSM']['Below_LCL'], '1')
        self.assertEqual(rows['OTHER<&>']['Above_UCL'], '1')
        self.assertEqual(rows['Unknown socket']['count'], '2')
        self.assertEqual(len(parsed.figures), 5)
        for figure in parsed.figures:
            socket = figure['layout']['meta']['socket'] or 'Unknown socket'
            self.assertEqual(sum(self.histogram(figure)['y']), int(rows[socket]['count']))
            self.assertIn('Socket: ' + socket, figure['layout']['meta']['export_title'])
        content = path.read_text(encoding='utf-8')
        self.assertEqual(content.count('class="test-statistics"'), 1)
        self.assertEqual(content.count('class="socket-result"'), 5)
        self.assertEqual(len(parsed.socket_groups), 5)
        self.assertTrue(all('open' in group for group in parsed.socket_groups))
        self.assertEqual({group['data-socket'] for group in parsed.socket_groups},
                 {'CLASSHOT', 'CSM', 'PHMHOT', 'OTHER<&>', ''})
        self.assertEqual(content.count('<summary title="Minimize socket plots">'), 5)
        self.assertEqual(content.count('</details>'), 6 + len(parsed.table_sections))
        self.assertIn('class="equipment-trace"', content)
        self.assertIn('Socket: OTHER&lt;&amp;&gt;', content)
        self.assertEqual(len(parsed.test_controls), 1)
        _, combined = self.export('diode')
        self.assertEqual(combined.socket_groups, [])
        self.assertEqual(len(combined.figures), 1)
        self.assertNotIn('Socket', combined.tables['Full-data product / test statistics'][0])

    def test_diode_test_controls_are_exact_unique_and_enabled_by_default(self):
        test = 'DIODE_"<&>'
        self.add(100, test=test, product='P1')
        self.add(101, test=test, product='P2')
        self.add(102, test=test + '_DELTA', product='P1')
        with patch.object(reports, '_BATCH', 1):
            path, parsed = self.export('diode')
        self.assertEqual([control['value'] for control in parsed.test_controls], [test, test + '_DELTA'])
        self.assertTrue(all('checked' in control for control in parsed.test_controls))
        self.assertIn('hidden', parsed.test_selector)
        self.assertEqual(len(parsed.test_sections), 3)
        self.assertEqual({section['data-test'] for section in parsed.test_sections}, {test, test + '_DELTA'})
        self.assertTrue(all('hidden' not in section for section in parsed.test_sections))
        self.assertEqual(len(parsed.figures), 3)
        self.assertEqual(len(parsed.tables['Full-data product / test statistics']), 3)
        content = path.read_text(encoding='utf-8')
        self.assertIn('value="' + escape(test) + '"', content)
        self.assertIn('class="test-toggle-all" checked', content)
        self.assertIn('No tests enabled.', content)

    def test_diode_empty_selection_has_no_test_controls(self):
        self.add(100, test='DIODE')
        _, parsed = self.export('diode', filters={'Test': []})
        self.assertIsNone(parsed.test_selector)
        self.assertEqual(parsed.test_controls, [])
        self.assertEqual(parsed.test_sections, [])
        self.assertEqual(parsed.figures, [])
        self.assertEqual(parsed.tables['Full-data product / test statistics'], [])

    def test_diode_chart_choices_preserve_full_data_and_limit_samples(self):
        for index, value in enumerate([0, 1, 2, 3, 4, 5, 6, 7, 100]):
            self.add(value, unit=f'u{index}', test='DIODE')
        path, parsed = self.export('diode', plot_limit=3)
        content = path.read_text(encoding='utf-8')
        self.assertIn('class="chart-type"', content)
        self.assertIn('Distribution</option>', content)
        self.assertIn('Plotly.react', content)
        self.assertIn("variants.scatter ? 'scatter' : 'histogram'", content)
        self.assertIn('selector.value = initialType', content)
        variants = reports._diode_views(self.store.connection, ' WHERE Test = ?', ['DIODE'],
                                       reports.go.Figure(parsed.figures[0]), 3)
        self.assertEqual(set(variants), {'scatter', 'line', 'distribution'})
        self.assertEqual(list(variants['scatter'].data[0].x), [1, 5, 9])
        self.assertEqual(list(variants['scatter'].data[0].y), [0, 4, 100])
        self.assertEqual(variants['line'].data[0].mode, 'lines+markers')
        distribution = variants['distribution']
        self.assertEqual(sum(distribution.data[0].y), 9)
        box = next(trace for trace in distribution.data if trace.type == 'box')
        self.assertEqual((list(box.q1), list(box.median), list(box.q3)), ([2], [4], [6]))
        self.assertEqual((list(box.lowerfence), list(box.upperfence)), ([0], [7]))
        self.assertEqual(distribution.layout.meta['outlier_count'], 1)
        self.assertEqual(distribution.layout.meta['count'], 9)

    def test_empty_diode_report_explains_missing_readings(self):
        path, parsed = self.export('diode')
        self.assertFalse(parsed.figures)
        self.assertIn('No matching finite diode readings.', path.read_text(encoding='utf-8'))

    def download(self, path, parsed, filename):
        link = next(link for link in parsed.links if link.endswith('/' + filename))
        self.assertFalse(Path(link).is_absolute())
        with (path.parent / link).open(encoding='utf-8', newline='') as stream:
            return list(csv.DictReader(stream, delimiter='\t' if filename.endswith('.tsv') else ','))

    def test_diode_exact_histograms_stats_and_separate_tests_products(self):
        for value in [0, 0, 10, 20, 100]:
            self.add(value, test='DIODE_VOLTS')
        for value in [5, 5, 5]:
            self.add(value, test='DIODE_AMPS')
        self.add(-500, test='DIODE_VOLTS', product='P2')
        _, parsed = self.export('diode', plot_limit=1)
        self.assertEqual(len(parsed.figures), 3)
        by_title = {fig['layout']['meta']['export_title']: fig for fig in parsed.figures}
        volts = by_title['P1 — DIODE_VOLTS']
        self.assertEqual(sum(self.histogram(volts)['y']), 5)
        self.assertEqual(self.histogram(volts)['y'][0], 2)
        self.assertEqual(self.histogram(volts)['y'][-1], 1)
        self.assertEqual(volts['layout']['meta']['mean'], 26)
        self.assertEqual(volts['layout']['meta']['minimum'], 0)
        self.assertEqual(volts['layout']['meta']['maximum'], 100)
        constant = by_title['P1 — DIODE_AMPS']
        self.assertEqual(self.histogram(constant)['y'], [3])
        self.assertEqual(constant['layout']['meta']['stddev_pop'], 0)
        self.assertEqual(list(parsed.tables), ['Full-data product / test statistics', 'Recipe Revision'])
        self.assertEqual(len(parsed.tables['Full-data product / test statistics']), 3)

    def test_diode_negative_extremes_and_bin_boundaries(self):
        for value in range(-20, 21):
            self.add(value, test='DIODE')
        _, parsed = self.export('diode')
        counts = self.histogram(parsed.figures[0])['y']
        self.assertEqual(sum(counts), 41)
        self.assertEqual(counts[0], 1)
        self.assertEqual(counts[-1], 2)

    def test_diode_configured_longest_match_limits(self):
        self.add(2, test='DIODE_VOLTS')
        limits = {'P1': [{'test': 'DIODE', 'LCL': -1, 'UCL': 100},
                         {'test': 'diode_volts', 'LCL': '0.5', 'UCL': '3'}]}
        _, parsed = self.export('diode', limits=limits)
        lines = parsed.figures[0]['layout']['shapes']
        self.assertEqual({shape['x0'] for shape in lines}, {0.5, 3.0})
        self.assertTrue(all(shape['x0'] == shape['x1'] for shape in lines))

    def test_socket_limit_overrides_defaults_and_longest_match(self):
        limits = {'P1': [
            {'test': 'DIODE', 'LCL': -10, 'UCL': 1000, 'sockets': {'CSM': {'LCL': 10, 'UCL': 20}}},
            {'test': 'DIODE_VOLTS', 'LCL': 0, 'UCL': 200,
             'sockets': {'CLASSHOT': {'LCL': 93, 'UCL': 107}, 'CSM': {'LCL': 70, 'UCL': 90},
                         'PHMHOT': {'LCL': None, 'UCL': None}, 'ROOM': {'LCL': 0, 'UCL': None}}},
        ], '_product_aliases': {'alias': 'P1'}}
        for socket, expected in [(' classhot ', [107, 93]), ('csm', [90, 70]),
                                 ('PHMHOT', [None, None]), ('ROOM', [None, 0]),
                                 ('CSM_EXTRA', [200, 0]), ('OTHER', [200, 0]),
                                 ('', [200, 0]), (None, [200, 0])]:
            with self.subTest(socket=socket):
                self.assertEqual(reports._spec_limits('alias', 'PREFIX_DIODE_VOLTS', limits, socket), expected)
        for socket in ('CLASSHOT', 'CSM', 'PHMHOT', 'OTHER'):
            self.add(100, test='DIODE_VOLTS', socket=socket)
        _, parsed = self.export('diode', limits=limits, per_socket=True)
        rows = {row['Socket']: row for row in parsed.tables['Full-data product / test statistics']}
        self.assertEqual(rows['CLASSHOT']['Above_UCL'], '0')
        self.assertEqual(rows['CSM']['Above_UCL'], '1')
        self.assertEqual(rows['PHMHOT']['Above_UCL'], 'N/A')
        self.assertEqual(rows['OTHER']['Above_UCL'], '0')
        for figure in parsed.figures:
            socket = figure['layout']['meta']['socket']
            expected = reports._spec_limits('P1', 'DIODE_VOLTS', limits, socket)
            self.assertEqual({shape['x0'] for shape in figure['layout'].get('shapes', [])},
                             {value for value in expected if value is not None})

    def test_configured_classhot_limits_do_not_apply_to_other_sockets(self):
        limits = json.loads((Path(__file__).parent / 'config' / 'Product_Test_Limits.json').read_text())
        for test, expected in [('PCS_SOT0', [107, 93]), ('PCS_SOT1', [107, 93]),
                               ('PCS_SOT2', [105, 95]), ('PCS_SOT3', [105, 93])]:
            self.assertEqual(reports._spec_limits('HX28C', test, limits, 'CLASSHOT'), expected)
            for socket in ('CSM', 'PHMHOT', 'OTHER', None):
                expected_socket = [0, -15] if test == 'PCS_SOT0' and socket == 'CSM' else [None, None]
                self.assertEqual(reports._spec_limits('HX28C', test, limits, socket), expected_socket)
        self.assertEqual(reports._spec_limits('C816_N2P', 'PCS_SOT0', limits), [25, 15])

    def test_diode_all_views_follow_configured_socket_limits(self):
        import re

        limits = json.loads((Path(__file__).parent / 'config' / 'Product_Test_Limits.json').read_text())
        for socket, value in [('CLASSHOT', 100), ('CSM', -10), ('PHMHOT', 100)]:
            self.add(value, test='PCS_SOT0', socket=socket)
        path, parsed = self.export('diode', limits=limits, limit_product='HX28C', per_socket=True)
        variants = [json.loads(value) for value in re.findall(
            r'<script class="plot-option"[^>]*>(.*?)</script>', path.read_text(encoding='utf-8'), re.S)]
        expected = {'CLASSHOT': (93, 107), 'CSM': (-15, 0), 'PHMHOT': (None, None)}
        for figure in [*parsed.figures, *variants]:
            metadata = figure['layout']['meta']
            lower, upper = expected[metadata['socket']]
            self.assertEqual((metadata['lcl'], metadata['ucl']), (lower, upper))
            shapes = figure['layout'].get('shapes', [])
            for bound in (lower, upper):
                if bound is not None:
                    self.assertTrue(any(shape.get('x0') == bound or shape.get('y0') == bound for shape in shapes))

    def test_diode_product_alias_uses_configured_class_limits(self):
        limits = json.loads((Path(__file__).parent / 'config' / 'Product_Test_Limits.json').read_text())
        test = limits['NVL_Class_HX28C'][0]['test']
        configured = limits['NVL_Class_HX28C'][0]['sockets']['CLASSHOT']
        expected = [configured['UCL'], configured['LCL']]
        for product in ('nvlHX', 'HX28C', 'NVL_Class_HX28C', 'NVLHX'):
            self.assertEqual(reports.resolve_limit_product(product, limits), 'NVL_Class_HX28C')
            self.assertEqual(reports._spec_limits(product, test, limits, socket='CLASSHOT'), expected)
        self.assertIsNone(reports.resolve_limit_product('Unknown_Product_4AAB2VC', limits))
        self.assertEqual(reports._spec_limits('unmapped', test, limits), (None, None))
        self.add(100, test=test, product='nvlHX', socket='CLASSHOT')
        _, parsed = self.export('diode', limits=limits, per_socket=True)
        self.assertEqual({shape['x0'] for shape in parsed.figures[0]['layout']['shapes']}, set(expected))

    def test_subnormal_diode_range(self):
        self.add(0, test='tiny')
        self.add(5e-324, test='tiny')
        _, parsed = self.export('diode')
        counts = self.histogram(parsed.figures[0])['y']
        self.assertEqual(sum(counts), 2)
        self.assertEqual(counts[0], 1)
        self.assertEqual(counts[-1], 1)

    def test_diode_single_plot_and_summary_with_profile_limit_product(self):
        limits = {'NVL_Class_HX28C': [{'test': 'DIODE', 'LCL': 95, 'UCL': 105}]}
        for test in ('DIODE_A', 'DIODE_B'):
            for index, value in enumerate((94, 95, 100, 105, 106)):
                self.add(value, unit=f'u{index}', test=test, product='Unknown_Product_4AAB2VC')
        _, parsed = self.export('diode', limits=limits, limit_product='NVL_Class_HX28C')
        self.assertEqual(len(parsed.figures), 2)
        self.assertEqual(list(parsed.tables), ['Full-data product / test statistics', 'Recipe Revision'])
        for figure in parsed.figures:
            self.assertEqual([trace['type'] for trace in figure['data']], ['bar'])
            self.assertEqual(len(figure['layout']['shapes']), 2)
            self.assertNotIn('xaxis2', figure['layout'])
            self.assertEqual(sum(self.histogram(figure)['y']), 5)
        for row in parsed.tables['Full-data product / test statistics']:
            self.assertEqual(row['Product_Name'], 'Unknown_Product_4AAB2VC')
            self.assertEqual(row['Limit_Product'], 'NVL_Class_HX28C')
            self.assertEqual((row['LCL'], row['UCL']), ('95.0', '105.0'))
            self.assertEqual((row['Below_LCL'], row['Above_UCL']), ('1', '1'))
            self.assertEqual((row['count'], row['units']), ('5', '5'))

    def test_selected_product_labels_table_without_changing_source(self):
        self.add(100, test='DIODE', product='Unknown_Product_4AAB2VC')
        _, parsed = self.export('diode', report_product='HX28C')
        self.assertEqual(parsed.tables['Full-data product / test statistics'][0]['Product_Name'], 'HX28C')
        metadata = parsed.figures[0]['layout']['meta']
        self.assertEqual(metadata['report_product'], 'HX28C')
        self.assertEqual(metadata['source_product'], 'Unknown_Product_4AAB2VC')
        self.assertEqual(self.store.values('Product_Name'), ['Unknown_Product_4AAB2VC'])

    def test_selected_product_does_not_inherit_source_limits(self):
        self.add(100, test='DIODE', product='P1')
        _, parsed = self.export('diode', report_product='U8C',
                                limits={'P1': [{'test': 'DIODE', 'LCL': 95, 'UCL': 105}]})
        row = parsed.tables['Full-data product / test statistics'][0]
        self.assertEqual(row['Product_Name'], 'U8C')
        self.assertEqual((row['LCL'], row['UCL'], row['Below_LCL']), ('N/A', 'N/A', 'N/A'))
        self.assertFalse(parsed.figures[0]['layout'].get('shapes'))

    def test_large_finite_diode_statistics_do_not_overflow(self):
        for value in (1e308, 1e308, 1e308):
            self.add(value, test='constant_extreme')
        for value in (-1e308, 1e308):
            self.add(value, test='opposite_extremes')
        _, parsed = self.export('diode')
        figures = {figure['layout']['meta']['export_title']: figure for figure in parsed.figures}
        constant = figures['P1 — constant_extreme']['layout']['meta']
        self.assertEqual(constant['mean'], 1e308)
        self.assertEqual(constant['stddev_pop'], 0)
        opposite = figures['P1 — opposite_extremes']
        self.assertEqual(opposite['layout']['meta']['mean'], 0)
        self.assertEqual(opposite['layout']['meta']['stddev_pop'], 1e308)
        self.assertEqual(sum(self.histogram(opposite)['y']), 2)

    def test_diode_quantiles_sample_sd_and_student_t_interval(self):
        for value in range(1, 6):
            self.add(value, test='DIODE')
        # Invalid cache values must not contribute to N, quantiles or spec flags.
        for value in (None, float('inf'), float('-inf'), float('nan')):
            self.add(value, test='DIODE')
        _, parsed = self.export('diode', plot_limit=2)
        figure = parsed.figures[0]
        meta = figure['layout']['meta']
        probabilities = [0, .005, .025, .10, .25, .50, .75, .90, .975, .995, 1]
        self.assertEqual(meta['quantile_probabilities'], probabilities)
        for actual, probability in zip(meta['quantiles'], probabilities):
            self.assertAlmostEqual(actual, 1 + 4 * probability)
        self.assertEqual(meta['count'], 5)
        self.assertAlmostEqual(meta['mean'], 3)
        self.assertAlmostEqual(meta['stddev_pop'], math.sqrt(2))
        self.assertAlmostEqual(meta['stddev_samp'], math.sqrt(2.5))
        self.assertAlmostEqual(meta['std_error'], math.sqrt(.5))
        # Student-t(df=4), not the narrower 1.96 * SE normal approximation.
        self.assertAlmostEqual(meta['ci95_lower'], 1.036756838522439, places=9)
        self.assertAlmostEqual(meta['ci95_upper'], 4.963243161477561, places=9)
        tables = [trace for trace in figure['data'] if trace['type'] == 'table']
        self.assertEqual(tables, [])
        summary = parsed.tables['Full-data product / test statistics'][0]
        self.assertEqual(summary['count'], '5')
        self.assertEqual(summary['LCL'], 'N/A')
        self.assertEqual(summary['Below_LCL'], 'N/A')
        self.assertIsNone(meta['missing_count'])
        self.assertIsNone(meta['below_lcl'])
        self.assertIsNone(meta['above_ucl'])

    def test_diode_histogram_layout_specs_and_full_data_counts(self):
        for value in [0, 1, 2, 3, 4, 5, 6, 7, 100]:
            self.add(value, test='DIODE')
        limits = {'P1': [{'test': 'DIODE', 'LCL': 1, 'UCL': 6}]}
        _, parsed = self.export('diode', limits=limits, source_note='Selected lots: L1, L2', plot_limit=2)
        figure = parsed.figures[0]
        layout, meta = figure['layout'], figure['layout']['meta']
        self.assertNotIn('width', layout)
        self.assertEqual(layout['height'], 440)
        self.assertTrue(layout['autosize'])
        self.assertEqual(meta['export_title'], 'P1 — DIODE | LCL = 1, UCL = 6')
        self.assertNotIn('OPERGROUP', json.dumps(figure))
        self.assertEqual(meta['source_note'], 'Selected lots: L1, L2')
        self.assertEqual((meta['below_lcl'], meta['above_ucl']), (1, 2))
        self.assertEqual((meta['lcl'], meta['ucl']), (1, 6))
        self.assertEqual(len(layout['shapes']), 2)
        self.assertEqual({s['xref'] for s in layout['shapes']}, {'x'})
        self.assertEqual({s['x0'] for s in layout['shapes']}, {1, 6})
        self.assertEqual({item['text'] for item in layout['annotations']}, {'LCL = 1', 'UCL = 6'})
        self.assertNotIn('xaxis2', layout)
        self.assertEqual(len(figure['data']), 1)
        histogram = self.histogram(figure)
        self.assertEqual(len(histogram['y']), 40)
        self.assertEqual(sum(histogram['y']), 9)

    def test_diode_histogram_is_deterministic_and_not_sampled(self):
        self.store.connection.execute('''INSERT INTO readings (Value, Product_Name, Test, Lot)
            SELECT i // 2, 'P1', 'DIODE', 'L1' FROM range(1000) t(i)''')
        _, first = self.export('diode', plot_limit=7)
        _, second = self.export('diode', plot_limit=1)
        figure = first.figures[0]
        self.assertEqual(figure, second.figures[0])
        self.assertEqual(figure['layout']['meta']['count'], 1000)
        self.assertEqual(len(figure['data']), 1)
        self.assertEqual(len(self.histogram(figure)['y']), 40)
        self.assertFalse(any(trace.get('fill') or trace.get('error_y') for trace in figure['data']))
        self.assertEqual(sum(self.histogram(figure)['y']), 1000)

    def test_diode_outlying_readings_are_included_in_histogram(self):
        self.store.connection.execute('''INSERT INTO readings (Value, Product_Name, Test)
            SELECT CASE WHEN i < 900 THEN 0 ELSE i END, 'P1', 'DIODE' FROM range(1000) t(i)''')
        _, parsed = self.export('diode', plot_limit=3)
        figure = parsed.figures[0]
        meta = figure['layout']['meta']
        self.assertEqual((meta['minimum'], meta['maximum']), (0, 999))
        self.assertEqual(self.histogram(figure)['y'][0], 900)
        self.assertEqual(sum(self.histogram(figure)['y']), 1000)

    def test_diode_singleton_and_constants_have_safe_undefined_statistics(self):
        self.add(7, test='singleton')
        for value in [0, 0, 0]:
            self.add(value, test='constant')
        _, parsed = self.export('diode', plot_limit=20)
        figures = {figure['layout']['meta']['export_title']: figure for figure in parsed.figures}
        singleton = figures['P1 — singleton']
        meta = singleton['layout']['meta']
        for key in ('stddev_samp', 'std_error', 'ci95_lower', 'ci95_upper'):
            self.assertIsNone(meta[key])
        self.assertEqual(meta['quantiles'], [7] * 11)
        self.assertEqual(meta['count'], 1)
        self.assertEqual(self.histogram(singleton)['y'], [1])
        constant = figures['P1 — constant']
        for key in ('stddev_samp', 'std_error', 'ci95_lower', 'ci95_upper'):
            self.assertEqual(constant['layout']['meta'][key], 0)
        for figure in figures.values():
            self.assertFalse(any(trace.get('mode') == 'lines' for trace in figure['data']))

    def test_diode_extreme_sample_statistics_quantiles_and_json_are_finite(self):
        groups = {'opposite': [-1e308, 1e308], 'overflow_sd': [-1.7e308, 1.7e308],
                  'subnormal': [0, 5e-324], 'tiny_same': [5e-324, 5e-324], 'same': [1e308, 1e308]}
        for test, values in groups.items():
            for value in values:
                self.add(value, test=test)
        _, parsed = self.export('diode')

        def assert_finite(value):
            if isinstance(value, dict):
                for child in value.values():
                    assert_finite(child)
            elif isinstance(value, list):
                for child in value:
                    assert_finite(child)
            elif isinstance(value, float):
                self.assertTrue(math.isfinite(value))

        for figure in parsed.figures:
            assert_finite(figure)
            meta = figure['layout']['meta']
            self.assertEqual(sum(self.histogram(figure)['y']), 2)
            self.assertEqual(meta['quantiles'][0], meta['minimum'])
            self.assertEqual(meta['quantiles'][-1], meta['maximum'])
            self.assertEqual(meta['quantiles'], sorted(meta['quantiles']))
            self.assertNotEqual(meta['display_scale'], 1)
            if meta['export_title'].endswith('opposite'):
                self.assertAlmostEqual(meta['stddev_samp'] / 1e308, math.sqrt(2))
                self.assertAlmostEqual(meta['std_error'] / 1e308, 1)
                self.assertAlmostEqual(meta['quantiles'][2] / 1e308, -.95)
                self.assertIsNone(meta['ci95_lower'])
                self.assertIsNone(meta['ci95_upper'])
            elif meta['export_title'].endswith('overflow_sd'):
                self.assertIsNone(meta['stddev_samp'])
                self.assertAlmostEqual(meta['std_error'] / 1.7e308, 1)
            elif meta['export_title'] == 'P1 — same':
                self.assertEqual(meta['ci95_lower'], 1e308)
                self.assertEqual(meta['ci95_upper'], 1e308)

    def test_diode_mixed_extreme_scales_preserve_quantiles_and_counts(self):
        for value in [5e-324, 1e-323, 1e308]:
            self.add(value, test='mixed')
        self.store.connection.execute('''INSERT INTO readings (Value, Product_Name, Test)
            SELECT CASE WHEN i < 900 THEN 1e-300 WHEN i < 999 THEN 2e-300 ELSE 1e308 END,
                'P1', 'tiny_center' FROM range(1000) t(i)''')
        _, parsed = self.export('diode', plot_limit=3)
        figures = {figure['layout']['meta']['export_title']: figure for figure in parsed.figures}
        mixed = figures['P1 — mixed']['layout']['meta']
        self.assertEqual(mixed['quantiles'][0], 5e-324)
        self.assertEqual(mixed['quantiles'][5], 1e-323)
        center = figures['P1 — tiny_center']['layout']['meta']
        self.assertEqual(center['quantiles'][5], 1e-300)
        self.assertEqual(center['count'], 1000)
        self.assertEqual((center['minimum'], center['maximum']), (1e-300, 1e308))

    def test_diode_long_title_and_source_are_escaped_with_full_export_metadata(self):
        test = '</script><img src=x onerror=alert(1)>' + 'long test ' * 40
        note = 'Selected lots: <L1> & L2 ' + 'context ' * 40
        self.add(5, test=test)
        limits = {'P1': [{'test': test, 'LCL': 4, 'UCL': 9}]}
        path, parsed = self.export('diode', source_note=note, limits=limits)
        figure = parsed.figures[0]
        title = figure['layout']['title']['text']
        content = path.read_text(encoding='utf-8')
        self.assertIn('<h2>' + escape(test) + '</h2>', content)
        self.assertNotIn('<img', title)
        self.assertIn('LCL: 4 | UCL: 9', content)
        self.assertLess(len(title), 450)
        self.assertEqual(figure['layout']['meta']['export_title'], f'P1 — {test} | LCL = 4, UCL = 9')
        self.assertEqual(figure['layout']['meta']['source_note'], note)
        self.assertNotIn('<img', path.read_text(encoding='utf-8'))

    def test_dashboard_wrapper_applies_socket_mode_to_all_categories(self):
        from shared_dashboard import export_dashboard

        test = 'IPC::ARR_ATOM::DTSMON_100'
        self.add(95, socket='CLASSHOT')
        self.add(97, socket='CSM')
        config = {test: {'COLUMN_NAMES': ['sensor_a'], 'CATEGORIES': ['thermal_diode', 'dts', 'dtsmon']}}
        for kind in ('thermal_diode', 'dts', 'dtsmon'):
            for per_socket in (True, False):
                with self.subTest(kind=kind, per_socket=per_socket):
                    path = export_dashboard(self.store, self.root / f'{kind}-{per_socket}',
                                            filters={'Test': [test]}, sensor_config=config, limits={},
                                            report_kind=kind, per_socket=per_socket)
                    parsed = ReportParser(path)
                    self.assertEqual(len(parsed.socket_groups), 2 if per_socket else 0)
                    self.assertEqual(len(parsed.figures), 2 if per_socket else 1)

    def test_dtsmon_sockets_have_independent_maxima_summaries_and_charts(self):
        self.add(130, socket='CLASSHOT', sensor='first_hot')
        self.add(130, socket='CLASSHOT', sensor='second_hot')
        self.add(100, socket='CLASSHOT', lot='L2')
        self.add(95, socket='CSM', sensor='cold')
        self.add(97, socket='CSM', sensor='warm')
        self.add(89, socket=None, sensor='unknown')
        self.add(88, socket='', sensor='blank')
        path, parsed = self.export(per_socket=True, plot_limit=10)
        rows = self.download(path, parsed, 'all_winners.tsv')
        self.assertEqual({row['Socket']: (float(row['DTSMax']), row['Hot_Sensor']) for row in rows},
                         {'CLASSHOT': (130, 'first_hot'), 'CSM': (97, 'warm'), 'Unknown socket': (89, 'unknown')})
        self.assertEqual(len(parsed.socket_groups), 3)
        self.assertTrue(all('open' in group for group in parsed.socket_groups))
        self.assertEqual(len(parsed.figures), 3)
        for figure in parsed.figures:
            meta = figure['layout']['meta']
            self.assertEqual(meta['maximum_keys'], ['Unit_ID', 'Test', 'Socket'])
            self.assertIn(meta['socket'], figure['layout']['title']['text'])
            self.assertEqual(figure['data'][0]['customdata'][0][3], meta['socket'])
        summary = {row['Socket']: row for row in parsed.tables['Per-lot summary']}
        self.assertEqual(summary['CLASSHOT']['over_gt120'], '1')
        self.assertEqual(summary['CSM']['within_90_120'], '1')
        self.assertEqual(summary['Unknown socket']['under_lt90'], '1')
        checks = {row['Check']: int(row['Result']) for row in parsed.tables['Verification']}
        self.assertEqual(checks['independent_grouped_max_keys'], 3)
        self.assertEqual(checks['max_or_key_mismatches'], 0)
        _, combined = self.export(per_socket=False)
        self.assertEqual(combined.socket_groups, [])
        self.assertEqual(combined.figures[0]['data'][0]['y'], [130])

    def test_dtsmon_maximum_ties_repeated_keys_and_lots(self):
        self.add(100, sensor='cold')
        self.add(130, sensor='source_z', source='z')
        self.add(130, sensor='first_a', source='a')
        self.add(130, sensor='second_a', source='a')
        self.add(50, sensor='other_lot', lot='L2')
        self.add(110, test='IPC::ARR_ATOM::DTSMON_100_SUFFIX')
        path, parsed = self.export()
        rows = self.download(path, parsed, 'all_winners.tsv')
        self.assertEqual(len(rows), 2)
        selected = next(row for row in rows if row['Lot'] == 'L1' and row['Test'].endswith('_100'))
        self.assertEqual(float(selected['DTSMax']), 130)
        self.assertEqual(selected['Hot_Sensor'], 'source_z')
        self.assertEqual(selected['Domain'], 'IPC::ARR_ATOM')
        self.assertEqual(parsed.figures[0]['layout']['meta']['plot_reference'],
                 'DTSDataBOT_rev7.calculate_dtsmax')
        self.assertEqual(parsed.figures[0]['layout']['meta']['maximum_keys'], ['Unit_ID', 'Test'])
        self.assertEqual(len(self.download(path, parsed, 'over_details.csv')), 1)
        self.assertEqual(len(self.download(path, parsed, 'under_details.csv')), 0)
        checks = {row['Check']: int(row['Result']) for row in parsed.tables['Verification']}
        self.assertEqual(checks['independent_grouped_max_keys'], 2)
        self.assertEqual(checks['max_or_key_mismatches'], 0)
        self.assertEqual(checks['duplicate_winner_keys'], 0)

    def test_exact_thresholds_not_setpoints_and_unsampled_statistics(self):
        values = [89.99, 90, 100, 120, 120.01]
        for index, value in enumerate(values):
            self.add(value, unit=f'u{index}')
        path, parsed = self.export(plot_limit=1, limits={'P1': [{'test': 'DTSMON', 'UCL': 100, 'LCL': 99}]})
        summary = parsed.tables['Per-lot summary'][0]
        for key, value in {'observations': '5', 'under_lt90': '1', 'within_90_120': '3',
                           'over_gt120': '1', 'min_dtsmax': '89.99', 'max_dtsmax': '120.01'}.items():
            self.assertEqual(summary[key], value)
        self.assertEqual(float(summary['under_percent']), 20)
        self.assertEqual(float(summary['within_percent']), 60)
        self.assertEqual(float(summary['over_percent']), 20)
        self.assertEqual(sum(len(fig['data'][0]['y']) for fig in parsed.figures), 1)
        self.assertEqual(len(self.download(path, parsed, 'all_winners.tsv')), 5)
        self.assertIn('1 displayed / 5 full-data maxima', path.read_text(encoding='utf-8'))

    def test_dtsmon_long_axis_labels_preserve_full_test_identity(self):
        names = [f'IPC::DOMAIN::DTS_VERY_LONG_TEST_NAME_WITH_ALL_ORIGINAL_PARTS_AND_SENSOR_SUFFIX_{index}'
                 for index in range(24)]
        for name in names:
            self.add(105, test=name)
        _, parsed = self.export(plot_limit=len(names))
        figure = parsed.figures[0]
        self.assertEqual(set(figure['data'][0]['x']), set(names))
        axis = figure['layout']['xaxis']
        self.assertLessEqual(len(axis['ticktext']), 12)
        self.assertTrue(all(len(label) <= 30 for label in axis['ticktext']))
        self.assertFalse(axis['automargin'])
        self.assertEqual(figure['layout']['margin']['b'], 135)

    def test_missing_keys_nonfinite_and_unknown_domain(self):
        for unit in ('', '  ', None):
            self.add(100, unit=unit)
        for test in ('', ' \t', None):
            self.add(100, unit='missing_test', test=test)
        for value in (float('inf'), float('-inf'), float('nan'), None):
            self.add(value, unit='nonfinite')
        self.add(95, test='bare', unit='valid', lot=None)
        self.add(96, test='bare', unit='valid', lot='')
        self.add(98, test='bare', unit='valid', lot=None)
        path, parsed = self.export()
        rows = self.download(path, parsed, 'all_winners.tsv')
        self.assertEqual(len(rows), 1)
        self.assertEqual({row['Domain'] for row in rows}, {'Unknown'})
        self.assertEqual({row['DTSMax'] for row in rows}, {'98.0'})
        excluded = parsed.tables['Excluded units / keys among cached finite readings']
        self.assertEqual(sum(int(row['cached_readings']) for row in excluded), 6)
        text = path.read_text(encoding='utf-8')
        self.assertIn('units with no valid source readings are unavailable', text)
        self.assertNotIn('source_units', text)

    def test_full_namespace_domains_and_actual_hover_data(self):
        self.add(100, test='IPC::ARR_ATOM::one:variant', unit='actual_u', sensor='actual_s')
        self.add(110, test='OTHER::ARR_ATOM::two', unit='second_u', sensor='second_s')
        self.add(115, test='IPC::ARR_ATOM::three', unit='third_u')
        _, parsed = self.export(plot_limit=2)
        self.assertEqual({fig['layout']['title']['text'] for fig in parsed.figures},
                         {'IPC::ARR_ATOM', 'OTHER::ARR_ATOM'})
        self.assertEqual(sum(len(fig['data'][0]['y']) for fig in parsed.figures), 2)
        for fig in parsed.figures:
            for unit, sensor, lot in fig['data'][0]['customdata']:
                self.assertIn(unit, ('actual_u', 'second_u', 'third_u'))
                self.assertIn(sensor, ('actual_s', 'second_s', 'sensor_a'))
                self.assertEqual(lot, 'L1')
        self.assertEqual(len(parsed.tables['Full test / lot summary']), 3)

    def test_exact_parameterized_filters(self):
        test = "IPC::DTSMON'; DROP TABLE readings; --"
        self.add(101, test=test)
        self.add(999, test=test + '_extra')
        self.add(999, test=test, lot='other')
        for kind in ('diode', 'dtsmon'):
            path, parsed = self.export(kind, filters={'Test': [test], 'Lot': 'L1'})
            if kind == 'diode':
                self.assertEqual(parsed.figures[0]['layout']['meta']['count'], 1)
                self.assertEqual(parsed.figures[0]['layout']['meta']['maximum'], 101)
            else:
                self.assertEqual(len(self.download(path, parsed, 'all_winners.tsv')), 1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM readings').fetchone()[0], 3)

    def test_empty_lists_do_not_leak_all_data(self):
        self.add(100)
        for kind in ('diode', 'dtsmon'):
            for filters in ({'Test': []}, {'Lot': ()}, {'Product_Name': [], 'Test': 'DTSMON'}):
                with self.subTest(kind=kind, filters=filters):
                    path, parsed = self.export(kind, filters=filters)
                    self.assertEqual(parsed.figures, [])
                    if kind == 'dtsmon':
                        self.assertEqual(self.download(path, parsed, 'all_winners.tsv'), [])
                    else:
                        self.assertEqual(parsed.tables['Full-data product / test statistics'], [])

    def test_empty_cache(self):
        for kind in ('diode', 'dtsmon'):
            _, parsed = self.export(kind)
            self.assertEqual(parsed.figures, [])

    def test_untrusted_text_html_json_and_csv_round_trip(self):
        attack = '</script><img src=x onerror=alert(1)>&\"\u2028'
        self.add(130, test='IPC::' + attack, unit=attack, sensor=attack, lot='comma,\nlot', product=attack)
        for kind in ('diode', 'dtsmon'):
            path, parsed = self.export(kind, source_note=attack)
            text = path.read_text(encoding='utf-8')
            self.assertNotIn(attack, text)
            self.assertNotIn('<img src=x', text)
            self.assertIn('&lt;/script&gt;', text)
            self.assertEqual(parsed.external_scripts, [])
            self.assertEqual(len(parsed.figures), 1)
            if kind == 'dtsmon':
                rows = self.download(path, parsed, 'all_winners.tsv')
                self.assertEqual(rows[0]['Unit_ID'], attack)
                self.assertEqual(rows[0]['Lot'], 'comma,\nlot')

    def test_real_plotly_is_embedded_offline(self):
        self.library.stop()
        self.add(100)
        path, parsed = self.export()
        self.assertGreater(path.stat().st_size, 1_000_000)
        self.assertEqual(parsed.external_scripts, [])
        self.assertIn('plotly.js', path.read_text(encoding='utf-8').lower())

    def test_cleanup_and_atomic_preservation_on_error(self):
        self.add(130)
        for kind in ('diode', 'dtsmon'):
            path, _ = self.export(kind)
            original = path.read_bytes()
            versions = set(path.parent.iterdir())
            with patch.object(reports, '_figure', side_effect=RuntimeError('chart failure')):
                with self.assertRaisesRegex(RuntimeError, 'chart failure'):
                    self.export(kind)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(set(path.parent.iterdir()), versions)
            self.assert_no_temp_tables()

    def test_cleanup_on_verification_error(self):
        self.add(100)
        with patch.object(reports, '_verification', side_effect=RuntimeError('verification failure')):
            with self.assertRaisesRegex(RuntimeError, 'verification failure'):
                self.export()
        self.assert_no_temp_tables()
        self.assertFalse((self.root / 'dtsmon' / 'dashboard.html').exists())

    def test_verification_detects_wrong_max_duplicate_and_missing_keys(self):
        self.add(100)
        connection = self.store.connection
        where, parameters = reports._selection(self.store, {})
        connection.execute('CREATE TEMP TABLE bad AS SELECT Unit_ID, Test, Lot, Value AS DTSMax FROM readings')
        try:
            connection.execute('UPDATE bad SET DTSMax = 99')
            with self.assertRaisesRegex(RuntimeError, 'verification failed'):
                reports._verification(connection, 'bad', where, parameters)
            connection.execute('UPDATE bad SET DTSMax = 100')
            connection.execute('INSERT INTO bad SELECT * FROM bad')
            with self.assertRaisesRegex(RuntimeError, 'verification failed'):
                reports._verification(connection, 'bad', where, parameters)
            connection.execute('DELETE FROM bad')
            with self.assertRaisesRegex(RuntimeError, 'verification failed'):
                reports._verification(connection, 'bad', where, parameters)
        finally:
            connection.execute('DROP TABLE bad')

    def test_multiple_streaming_batches(self):
        self.store.connection.execute('''INSERT INTO readings (Value, Unit_ID, Test, Lot, Measurement_Point)
            SELECT i, 'unit_' || i, 'IPC::DTSMON', 'L1', 'sensor_' || i FROM range(800) t(i)''')
        path, parsed = self.export(plot_limit=3)
        self.assertEqual(len(self.download(path, parsed, 'all_winners.tsv')), 800)
        self.assertEqual(parsed.tables['Per-lot summary'][0]['observations'], '800')
        _, diode = self.export('diode')
        self.assertEqual(list(diode.tables), ['Full-data product / test statistics', 'Recipe Revision'])
        self.assertEqual(diode.tables['Full-data product / test statistics'][0]['units'], '800')
        self.assertEqual(sum(self.histogram(diode.figures[0])['y']), 800)

    def test_diode_multiple_test_batches_share_one_summary(self):
        self.store.connection.execute('''INSERT INTO readings (Value, Product_Name, Test)
            SELECT 100, 'P1', 'DIODE_' || i FROM range(270) t(i)''')
        _, parsed = self.export('diode')
        self.assertEqual(len(parsed.figures), 270)
        self.assertEqual(list(parsed.tables), ['Full-data product / test statistics', 'Recipe Revision'])
        self.assertEqual(len(parsed.tables['Full-data product / test statistics']), 270)

    def test_invalid_limit_and_filter_identifiers(self):
        for kind in ('diode', 'dtsmon'):
            for limit in (0, -1, 20001, True, 1.5):
                with self.assertRaises(ValueError):
                    self.export(kind, plot_limit=limit)
            with self.assertRaises(ValueError):
                self.export(kind, filters={'Test) OR TRUE --': 'anything'})

    def test_only_bounded_fetches(self):
        self.add(100)
        connection = self.store.connection
        calls = []

        class BoundedConnection:
            @property
            def description(self):
                return connection.description

            def execute(self, *args):
                connection.execute(*args)
                return self

            def fetchone(self):
                return connection.fetchone()

            def fetchmany(self, size):
                if size > reports._BATCH:
                    raise AssertionError('Unbounded report fetch')
                calls.append(size)
                return connection.fetchmany(size)

        self.store.connection = BoundedConnection()
        try:
            for kind in ('diode', 'dtsmon'):
                getattr(reports, 'export_' + kind)(
                    self.store, self.root / kind, filters={}, sensor_config={}, limits={},
                    source_note='', plot_limit=2)
        finally:
            self.store.connection = connection
        self.assertTrue(calls)
        self.assert_no_temp_tables()


if __name__ == '__main__':
    unittest.main()