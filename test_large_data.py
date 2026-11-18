import csv
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openpyxl import Workbook
import duckdb

from large_data_store import ThermalStore


class ThermalStoreTests(unittest.TestCase):
    def test_equipment_header_aliases_and_socket_precedence(self):
        from large_data_store import _normalize_header, _socket_value

        for header in ('Thermal_Head_Id', 'thermal-head-id', 'ThermalHead'):
            self.assertEqual(_normalize_header(header), 'Thermal Head Id')
        for header in ('Unit_Tester_Id', 'TesterId', 'tester'):
            self.assertEqual(_normalize_header(header), 'Unit Tester Id')
        self.assertEqual(_normalize_header('SubFlow Step'), 'SubFlowStep')
        self.assertEqual(_normalize_header('Operation SubFlowStep Latest Flag'), 'Operation SubFlowStep Latest Flag')
        self.assertEqual(_socket_value({'Socket': ' phm_hot ', 'ProcessStep': 'CSM'}), 'PHMHOT')
        self.assertEqual(_socket_value({'Socket': 'N/A', 'ProcessStep': 'phm cold'}), 'PHMCOLD')
        self.assertEqual(_socket_value({'Socket': None, 'SubFlowStep': 'classhot'}), 'CLASSHOT')
        self.assertEqual(_socket_value({'Socket': '0', 'ProcessStep': 'CSM'}), '0')
        self.assertEqual(_socket_value({'Operation SubFlowStep Latest Flag': 'Y'}), '')

    def test_aqua_operation_group_socket_fallback(self):
        from large_data_store import _normalize_header, _socket_value

        self.assertEqual(_normalize_header('oper_group'), 'OPERGROUP')
        for socket in ('CLASSHOT', 'CSM', 'PHMHOT', 'PHMCOLD', 'PHMROOM'):
            self.assertEqual(_socket_value({'OPERGROUP': '6248_' + socket}), socket)
        self.assertEqual(_socket_value({'OPERGROUP': '6248_phm_room'}), 'PHMROOM')
        self.assertEqual(_socket_value({'Socket': 'N/A', 'OPERGROUP': '6248_CLASSHOT'}), 'CLASSHOT')
        self.assertEqual(_socket_value({'ProcessStep': 'CSM', 'OPERGROUP': '6248_CLASSHOT'}), 'CSM')
        for invalid in ('6248', 'CLASSHOT', 'not_an_operation_CLASSHOT', '6248_', '6248_N/A'):
            self.assertEqual(_socket_value({'OPERGROUP': invalid}), '')

    def test_aqua_csv_and_workbook_preserve_equipment_per_reading(self):
        self.source.unlink()
        header = ['VISUAL_ID', 'OPERGROUP', 'Lot', 'Thermal Head Id', 'Unit Tester Id',
                  'Unit Tester Site Id', 'TEST_NAME', 'TEST_RESULT', 'TEST_RESULT_NUMERIC']
        rows = [
            ['unit-1', '6248_CLASSHOT', 'lot-1', 'head-1', 'tester-1', 'A102', 'A', '88|87', None],
            ['unit-2', '6248_PHMCOLD', 'lot-1', 'head-2', 'tester-2', 'A101', 'A', None, 100.8],
        ]
        for extension in ('csv', 'xlsx'):
            with self.subTest(extension=extension):
                source = self.inputs / ('aqua.' + extension)
                if extension == 'csv':
                    with source.open('w', newline='', encoding='utf-8') as stream:
                        csv.writer(stream).writerows([header, *rows])
                else:
                    workbook = Workbook()
                    for row in [header, *rows]:
                        workbook.active.append(row)
                    workbook.save(source)
                    workbook.close()
                self.refresh()
                actual = self.store.connection.execute('''SELECT Socket, Thermal_Head_Id,
                    Unit_Tester_Id, Unit_Tester_Site_Id, Unit_ID, count(*) FROM readings
                    GROUP BY ALL ORDER BY Socket''').fetchall()
                self.assertEqual(actual, [
                    ('CLASSHOT', 'head-1', 'tester-1', 'A102', 'unit-1', 2),
                    ('PHMCOLD', 'head-2', 'tester-2', 'A101', 'unit-2', 1),
                ])
                source.unlink()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.inputs = self.root / "input"
        self.inputs.mkdir()
        self.source = self.inputs / "thermal.csv"
        self.write_rows([("A", "1|bad|3|nan|inf", "001", "8PF6CV"), ("A", "5|7", "002", "other")])
        self.store = ThermalStore(self.root / "data.duckdb", batch_size=2)
        self.config = {"A": {"COLUMN_NAMES": ["first", "second", "third"]}}
        self.products = {"8PF6CV": "NVL"}

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def write_rows(self, rows):
        with self.source.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["TEST_NAME", "TEST_RESULT", "VISUAL_ID", "DevRevStep"])
            writer.writerows(rows)

    def refresh(self):
        return self.store.refresh(self.inputs, self.config, self.products)

    def test_streamed_values_and_cache(self):
        with patch.object(self.store, "_insert", wraps=self.store._insert) as insert:
            self.assertEqual(self.refresh(), (1, 3))
            self.assertTrue(all(len(call.args[0]) <= 2 for call in insert.call_args_list))
        self.assertEqual(self.store.summary()["readings"], 4)
        self.assertEqual(self.store.summary()["mean"], 4)
        self.assertEqual(self.store.summary({"Product_Name": "NVL", "Unit_ID": "001"})["readings"], 2)
        self.assertEqual(self.store.summary({"Measurement_Point": "third"})["mean"], 3)
        with patch("large_data_store._source_rows", side_effect=AssertionError("Cache was not reused")):
            self.refresh()

    def test_changed_removed_and_configuration(self):
        self.refresh()
        self.assertTrue(self.store.is_current(self.inputs, self.config, self.products))
        self.store.invalidate()
        self.assertFalse(self.store.is_current(self.inputs, self.config, self.products))
        self.refresh()
        self.config["A"]["COLUMN_NAMES"][0] = "renamed"
        self.refresh()
        self.assertEqual(self.store.summary({"Measurement_Point": "renamed"})["readings"], 2)
        self.write_rows([("A", "11", "003", "8PF6CV")])
        self.refresh()
        self.assertEqual(self.store.summary()["mean"], 11)
        self.source.unlink()
        self.refresh()
        self.assertEqual(self.store.summary()["readings"], 0)

    def test_html_snapshot_uses_output_folder_and_bounded_reads(self):
        from export_dashboard_html import export_dashboard
        self.refresh()
        with patch.object(self.store, 'plot_rows', wraps=self.store.plot_rows) as sample, patch.object(
            self.store, 'statistics', side_effect=AssertionError('Repeated paged aggregation used')
        ):
            destination = export_dashboard(self.store, self.root / 'output')
        self.assertEqual(destination, self.root / 'output' / 'dashboard.html')
        content = destination.read_text(encoding='utf-8')
        self.assertIn('4 readings', content)
        self.assertIn('Exact lot statistics', content)
        self.assertIn('plotly.js', content)
        self.assertIn('"type":"scatter"', content)
        self.assertIn('IntersectionObserver', content)
        self.assertIn('type="application/json" class="plot-data"', content)
        self.assertTrue(sample.called)
        self.assertFalse(destination.with_suffix('.html.tmp').exists())

    def test_streamed_statistics_match_pages_and_preserve_cursor(self):
        self.refresh()
        expected, _pages = self.store.statistics(by_lot=True)
        actual = []
        for batch in self.store.iter_statistics(batch_size=1, by_lot=True):
            self.assertEqual(len(batch), 1)
            self.assertEqual(self.store.summary()['readings'], 4)
            actual.extend(batch.to_dict('records'))
        self.assertEqual(actual, expected)
        self.assertEqual(list(self.store.iter_statistics({'Test': 'missing'})), [])
        with self.assertRaises(ValueError):
            list(self.store.iter_statistics(batch_size=0))

    def test_html_configurable_point_budget(self):
        from export_dashboard_html import export_dashboard
        self.refresh()
        with patch.object(self.store, 'plot_rows', wraps=self.store.plot_rows) as sample:
            destination = export_dashboard(self.store, self.root / 'output', product='NVL', plot_limit=1)
        self.assertEqual(sample.call_args.kwargs['limit'], 1)
        content = destination.read_text(encoding='utf-8')
        self.assertIn('1 displayed / 2 readings', content)
        self.assertIn('Charts show up to 1 sampled readings', content)
        for limit in [0, 20001]:
            with self.assertRaises(ValueError):
                export_dashboard(self.store, self.root / 'output', plot_limit=limit)

    def test_html_sensor_labels_have_room(self):
        from export_dashboard_html import export_dashboard
        self.write_rows([('A', '|'.join(['1'] * 285), '001', '8PF6CV')])
        self.refresh()
        destination = export_dashboard(self.store, self.root / 'output', product='NVL')
        self.assertIn('min-width:3500px', destination.read_text(encoding='utf-8'))

    def test_html_lazy_payload_escapes_script_content(self):
        from export_dashboard_html import export_dashboard
        self.write_rows([('A', '1', '</script><script>alert(1)</script>', '8PF6CV')])
        self.refresh()
        destination = export_dashboard(self.store, self.root / 'output', product='NVL')
        content = destination.read_text(encoding='utf-8')
        self.assertNotIn('</script><script>alert(1)</script>', content)

    def test_html_product_filter_and_template_limits(self):
        from export_dashboard_html import export_dashboard, sensor_figure
        self.refresh()
        selected = {'Product_Name': 'NVL', 'Test': 'A'}
        stats = self.store.test_stats(selected).loc['A']
        figure = sensor_figure(self.store.plot_rows(selected), 'A', stats, (135, 50), ['first', 'third'])
        self.assertEqual([shape.y0 for shape in figure.layout.shapes],
                         [2, 2 + 10 * 2 ** 0.5, 2 - 10 * 2 ** 0.5, 135, 50])
        self.assertEqual(figure.layout.xaxis.categoryarray, ('first', 'third'))
        self.assertEqual(figure.data[0].marker.color, 'blue')
        with patch.object(self.store, 'plot_rows', wraps=self.store.plot_rows) as sample:
            destination = export_dashboard(self.store, self.root / 'output', product='NVL',
                                           source_note='Cached data <not refreshed>')
        content = destination.read_text(encoding='utf-8')
        self.assertIn('Cached data &lt;not refreshed&gt;', content)
        self.assertIn('2 readings; 1 products;', content)
        self.assertNotIn('Unknown_Product_other', content)
        self.assertTrue(all(call.args[0]['Product_Name'] == 'NVL' for call in sample.call_args_list))
        with self.assertRaisesRegex(ValueError, 'No readings'):
            export_dashboard(self.store, self.root / 'output', product='missing')

    def test_original_interface_queries_are_exact_and_plots_bounded(self):
        self.refresh()
        self.assertEqual(self.store.overview(), dict(readings=4, products=2, tests=1, units=2, lots=1))
        self.assertEqual(self.store.test_stats().loc['A', 'count'], 4)
        self.assertEqual(self.store.test_stats().loc['A', 'mean'], 4)
        self.assertEqual(len(self.store.plot_rows(limit=2)), 2)
        self.assertIn('BOM', self.store.plot_rows().columns)
        self.assertEqual(self.store.values('Unit_ID'), ['001', '002'])
        boxes = self.store.boxes({'Product_Name': 'NVL'})
        self.assertEqual(boxes['mean'].sum(), 4)
        self.assertEqual(self.store.commonality({}, '', 'Lot')['Failed Units'].sum(), 2)
        rows, total, repeated = self.store.repeat_failures({})
        self.assertTrue(rows.empty)
        self.assertEqual((total, repeated), (0, 0))

    def test_original_dashboard_views_and_comparison(self):
        import atlas_thermal_dashboard_optimized as dashboard
        self.refresh()
        store = dashboard.DataStore()
        store.backend = self.store
        store.loaded = True
        store.overview = self.store.overview()
        store.df = self.store.plot_rows(limit=1)
        with patch.object(dashboard, 'data_store', store):
            for route in ['/', '/product', '/test-analysis', '/data-filter', '/plot-setting', '/configuration', '/data-upload']:
                self.assertIsNotNone(dashboard.display_page(route))
            self.assertIsNotNone(dashboard.update_dashboard_product_cards('/', {}))
            self.assertIsNotNone(dashboard.update_test_product_matrix('/', {}))
            details = dashboard.update_product_details('NVL', 10, {}, 5, None, None, None, None, None, None, None)
            self.assertIsNotNone(details)
            callback = dashboard.app.callback_map['product-details-content.children']
            values = ['NVL', 10, {}, 5] + [None] * 9
            response = dashboard.app.server.test_client().post('/_dash-update-component', json={
                'output': 'product-details-content.children',
                'outputs': {'id': 'product-details-content', 'property': 'children'},
                'inputs': [dict(item, value=value) for item, value in zip(callback['inputs'], values)],
                'state': [], 'changedPropIds': ['product-selector.value'],
            })
            self.assertEqual(response.status_code, 200, response.data[:500])
            result = dashboard.update_lot_comparison('__ALL__', None, None, 'A', None, None,
                                                      None, None, None, {}, 0, [])
            self.assertEqual(sum(row['readings'] for row in result[0]), 4)
            self.assertEqual(sum(len(trace.x) for trace in result[2].data), 4)
            self.assertIn('Missing source metadata', result[3])
            self.assertIsNotNone(dashboard.update_repeat_failures('__ALL__', None, 2, {}, None, None, None, None))
            self.assertEqual(dashboard.update_bin_fbin_options('__ALL__', None, None, None, None), ([], None))
            self.assertEqual(dashboard.update_lot_comparison('__ALL__', None, None, 'A', None, None,
                                                             None, None, ['Other'], {}, 0, [])[0], [])

    def test_original_loader_never_materializes_full_dataframe(self):
        import atlas_thermal_dashboard_optimized as dashboard
        store = dashboard.DataStore()
        store.backend = self.store
        with patch.object(dashboard, 'DATA_FOLDER', str(self.inputs)), patch.object(
            dashboard, 'load_config_from_folder', return_value=self.config
        ), patch.object(dashboard, 'load_product_limits', return_value={}), patch.object(
            dashboard, 'load_data_from_folder', side_effect=AssertionError('Unbounded loader used')
        ):
            self.assertTrue(store.load_data())
            self.assertEqual(store.overview['readings'], 4)
            self.assertEqual(len(store.df), 1)
            self.assertIn('4 readings', store.load_status)

    def test_box_fences_exclude_outliers_and_failure_totals(self):
        with self.source.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(['TEST_NAME', 'TEST_RESULT', 'VISUAL_ID', 'FUNCTIONAL_BIN', 'Lot'])
            writer.writerows([['A', value, '001', '2', '01'] for value in [1, 2, 3, 4, 100]])
            writer.writerow(['B', 3, '001', '2', '01'])
        self.refresh()
        box = self.store.boxes({'Test': 'A'}).iloc[0]
        self.assertEqual(box['maximum'], 4)
        rows, total, repeated = self.store.repeat_failures({}, 2)
        self.assertEqual((total, repeated), (1, 1))
        self.assertEqual(rows.iloc[0]['Failure_Count'], 2)

    def test_lot_comparison_preserves_product_bom_pkg_and_sensor(self):
        with self.source.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["test_name", "test_result", "lot", "BOM", "PKG", "PRODUCT"])
            writer.writerows([
                ["A", "1|10", "001", "B1", "P1", "Product A"],
                ["A", "3|20", "001", "B1", "P1", "Product A"],
                ["A", "8|30", "002", "B1", "P1", "Product A"],
                ["A", "100", "001", "B2", "P1", "Product A"],
                ["A", "200", "001", "B1", "P2", "Product A"],
                ["A", "300", "001", "B1", "P1", "Product B"],
                ["Other", "999", "001", "B1", "P1", "Product A"],
            ])
        self.refresh()
        filters = {"Product_Name": "Product A", "BOM": "B1", "PKG": "P1", "Test": "A",
                   "Measurement_Point": "first", "Lot": ["001", "002"]}
        rows, pages = self.store.statistics(filters, by_lot=True)
        self.assertEqual(pages, 1)
        self.assertEqual([row["Lot"] for row in rows], ["001", "002"])
        self.assertEqual([row["mean"] for row in rows], [2, 8])
        self.assertEqual([row["readings"] for row in rows], [2, 1])
        self.assertEqual(rows[0]["stddev"], 1)
        all_rows, pages = self.store.statistics({"Test": "A"}, by_lot=True, page_size=2)
        self.assertEqual(len(all_rows), 2)
        self.assertEqual(pages, 4)
        self.assertEqual(self.store.summary({"Lot": ["' OR 1=1 --"]})["readings"], 0)
        self.assertEqual(self.store.options("BOM", {"PKG": "P2"})[0]["value"], "B1")

    def test_missing_bom_pkg_are_blank_not_inferred(self):
        self.refresh()
        rows, _pages = self.store.statistics(by_lot=True)
        self.assertTrue(all(row["BOM"] == "" and row["PKG"] == "" for row in rows))

    def test_existing_database_schema_is_upgraded_and_reimported(self):
        self.refresh()
        self.store.connection.execute("ALTER TABLE readings DROP COLUMN BOM")
        self.store.connection.execute("ALTER TABLE readings DROP COLUMN PKG")
        self.store.connection.execute("UPDATE sources SET fingerprint = 'old-version'")
        self.store.close()
        self.store = ThermalStore(self.root / "data.duckdb", batch_size=2)
        with patch.object(self.store, "_insert", wraps=self.store._insert) as insert:
            self.refresh()
            self.assertTrue(insert.called)
        self.assertEqual(self.store.summary()["readings"], 4)
        self.assertEqual(self.store.statistics(by_lot=True)[0][0]["PKG"], "")

    def test_failed_refresh_rolls_back(self):
        self.refresh()
        self.write_rows([("A", "100|200|300", "003", "8PF6CV")])
        with patch.object(self.store, "_insert", side_effect=RuntimeError("simulated failure")):
            with self.assertRaises(RuntimeError):
                self.refresh()
        self.assertEqual(self.store.summary()["readings"], 0)
        self.refresh()
        self.assertEqual(self.store.summary()["mean"], 200)

    def test_commit_error_is_not_masked_by_rollback(self):
        real_connection = self.store.connection
        mocked = Mock(wraps=real_connection)

        def execute(query, *arguments):
            if query == "COMMIT":
                real_connection.execute("ROLLBACK")
                raise duckdb.IOException("disk full during commit")
            return real_connection.execute(query, *arguments)

        mocked.execute.side_effect = execute
        with patch.object(self.store, "connection", mocked):
            with self.assertRaisesRegex(duckdb.IOException, "disk full during commit"):
                self.refresh()
        self.assertEqual(self.store.summary()["readings"], 0)

    def test_filter_is_parameterized(self):
        self.refresh()
        self.assertEqual(self.store.summary({"Test": "' OR 1=1 --"})["readings"], 0)
        with self.assertRaises(ValueError):
            self.store.summary({"Value": 1})

    def test_bounded_queries_and_exact_export(self):
        self.refresh()
        self.assertEqual(len(self.store.sample(limit=2)), 2)
        self.assertEqual(self.store.sample({"Unit_ID": "001"})["Value"].sum(), 4)
        rows, pages = self.store.statistics(page_size=1, sort_by=[{"column_id": "mean", "direction": "desc"}])
        self.assertEqual(pages, 4)
        self.assertEqual(rows[0]["mean"], 7)
        self.assertEqual(self.store.options("Unit_ID", search="002"), [{"label": "002", "value": "002"}])
        target = self.root / "selected.csv"
        self.store.export(target, {"Unit_ID": "001"})
        with target.open(newline="") as stream:
            exported = list(csv.DictReader(stream))
        self.assertEqual(len(exported), 2)
        self.assertEqual(exported[0]["Unit_ID"], "001")
        self.store.export(self.root / "all.parquet")
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM read_parquet(?)", [str(self.root / "all.parquet")]).fetchone()[0], 4)

    def test_excel_and_latin1_text(self):
        workbook = Workbook()
        workbook.active.append(["TEST_NAME", "TEST_RESULT", "VISUAL_ID", "Package"])
        workbook.active.append(["Excel", "2|4", "009", "PKG-A"])
        workbook.save(self.inputs / "book.xlsx")
        workbook.close()
        (self.inputs / "legacy.txt").write_text("TEST_NAME\tTEST_RESULT\tVISUAL_ID\nCaf\xe9\t6\t008\n", encoding="latin-1")
        self.refresh()
        self.assertEqual(self.store.summary()["readings"], 7)
        self.assertEqual(self.store.summary({"Test": "Caf\xe9"})["mean"], 6)
        self.assertEqual(self.store.summary({"Test": "Excel"})["mean"], 3)
        self.assertEqual(self.store.summary({"PKG": "PKG-A"})["readings"], 2)

    def test_invalid_input_preserves_existing_data(self):
        self.refresh()
        self.source.write_text("wrong,header\n1,2\n")
        with self.assertRaisesRegex(ValueError, "TEST_NAME"):
            self.refresh()
        self.assertEqual(self.store.summary()["readings"], 4)

    def test_options_are_bounded_and_searchable(self):
        self.write_rows([("A", "1", str(index), "other") for index in range(250)])
        self.refresh()
        self.assertEqual(len(self.store.options("Unit_ID")), 200)
        self.assertEqual(self.store.options("Unit_ID", search="249"), [{"label": "249", "value": "249"}])

    def test_dashboard_layout_callbacks_and_streaming_download(self):
        from atlas_thermal_dashboard_large import create_app

        self.refresh()
        app = create_app(self.store, self.inputs, lambda: self.config, self.products, self.root, initial_refresh=False)
        client = app.server.test_client()
        self.assertEqual(client.get("/").status_code, 200)
        self.assertEqual(client.get("/_dash-layout").status_code, 200)
        self.assertEqual(client.get("/_dash-dependencies").status_code, 200)
        response = client.get("/large-export/csv?Unit_ID=001")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data.decode().splitlines()), 3)
        response.close()
        self.assertEqual(client.get("/large-export/csv?Value=1").status_code, 400)
        chart = next(item["callback"].__wrapped__ for key, item in app.callback_map.items() if "large-chart.figure" in key)
        metrics, figure, caption = chart(1, {}, "histogram", 100)
        self.assertEqual(len(metrics), 5)
        self.assertEqual(len(figure.data[0].x), 4)
        self.assertIn("4 matching readings", caption)
        comparison = next(item["callback"].__wrapped__ for key, item in app.callback_map.items() if "large-comparison-chart.figure" in key)
        rows, pages, figure, caption = comparison(1, {"Test": "A"}, 0, [])
        self.assertEqual(len(rows), 4)
        self.assertEqual(pages, 1)
        self.assertEqual(sum(len(trace.x) for trace in figure.data), 4)
        self.assertIn("Missing source metadata: BOM, PKG", caption)
        self.assertEqual(comparison(1, {}, 0, [])[0], [])
        self.assertEqual(comparison(1, {"Test": "absent"}, 0, [])[3], "No matching readings")
        response = client.get("/large-export/csv?Unit_ID=001&Unit_ID=002")
        self.assertEqual(len(response.data.decode().splitlines()), 5)
        response.close()


class LauncherTests(unittest.TestCase):
    def test_default_launcher_preserves_original_interface(self):
        import run_dashboard

        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            app = Mock()
            data_store = Mock(load_status="Test data ready")
            app.run.side_effect = lambda **kwargs: data_store.load_data.assert_called_once_with()
            dashboard = SimpleNamespace(app=app, data_store=data_store, OUTPUT_FOLDER=temporary,
                CONFIG_FOLDER=temporary, DATA_FOLDER=temporary, PERSISTENT_DATA_FOLDER=temporary)
            try:
                os.chdir(temporary)
                with patch.dict("sys.modules", {"atlas_thermal_dashboard_optimized": dashboard}), patch(
                    "sys.argv", ["run_dashboard.py", "--no-browser"]
                ):
                    run_dashboard.main()
                self.assertEqual(Path.cwd(), Path(run_dashboard.__file__).resolve().parent)
                app.run.assert_called_once_with(debug=False, host="127.0.0.1", port=8050, use_reloader=False)
            finally:
                os.chdir(previous)

    def test_optional_compact_launcher_uses_disk_without_loading_dataframe(self):
        import run_dashboard

        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            dashboard = SimpleNamespace(app=Mock(), data_store=Mock(), OUTPUT_FOLDER=temporary,
                CONFIG_FOLDER=temporary, DATA_FOLDER=temporary, PERSISTENT_DATA_FOLDER=temporary,
                PRODUCT_MAPPING={}, load_config_from_folder=Mock(return_value={}))
            application = Mock()
            try:
                with patch.dict("sys.modules", {"atlas_thermal_dashboard_optimized": dashboard}), patch(
                    "sys.argv", ["run_dashboard.py", "--large-data", "--no-browser"]
                ), patch("large_data_store.ThermalStore") as store, patch(
                    "atlas_thermal_dashboard_large.create_app", return_value=application
                ) as create_app:
                    run_dashboard.main()
                store.assert_called_once()
                create_app.assert_called_once()
                dashboard.data_store.load_data.assert_not_called()
                application.run.assert_called_once_with(debug=False, host="127.0.0.1", port=8050, use_reloader=False)
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()