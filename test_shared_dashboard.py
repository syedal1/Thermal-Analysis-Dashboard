"""Offline contract tests with real DuckDB and filtered exporter integration."""

import copy
import csv
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urljoin

import profiles
import shared_dashboard as shared


TEMPLATE = '''<!doctype html><html><body>
<button role="tab">Report Library</button><button role="tab">Report Analysis</button>
<script id="portal-data" type="application/json">__PORTAL_DATA__</script>
</body></html>'''


class DesktopReleaseTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32', 'Windows extended paths')
    def test_report_download_from_extended_length_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path('\\\\?\\' + str(Path(temporary) / ('workspace-' + 'long' * 35)))
            publish = root / 'output' / 'shared'
            relative = 'approved-report/' + 'a' * 32 + '/thermal_diode/dashboard.html'
            target = publish / 'reports' / relative
            target.parent.mkdir(parents=True)
            target.write_text('<html>Socket baseline</html>', encoding='utf-8')
            try:
                app = shared.create_app(root, publish)
                with app.test_client().get('/reports/' + relative) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(b'Socket baseline', response.data)
                self.assertEqual(app.test_client().get('/reports/missing.html').status_code, 404)
            finally:
                shutil.rmtree(root)

    def test_desktop_opens_baseline_and_blocks_changes_to_approved_report(self):
        from desktop_release import launch_desktop

        server = Mock(server_port=8099)
        server.serve_forever.side_effect = KeyboardInterrupt
        with tempfile.TemporaryDirectory() as temporary:
            app = shared.create_app(Path(temporary), Path(temporary) / 'published')
            self._check_desktop_startup(temporary, app, server, launch_desktop)

    def _check_desktop_startup(self, temporary, app, server, launch_desktop):
        with patch('desktop_release.prepare_workspace', return_value=(
                Path(temporary), {'baseline_slug': 'approved', 'baseline_title': 'Socket baseline'})), patch(
                'shared_dashboard.publish_portal'), patch('shared_dashboard.create_app', return_value=app), patch(
                'werkzeug.serving.make_server', side_effect=[SystemExit(1), server]) as make_server, patch(
                'webbrowser.open') as browser, patch('desktop_release.os.chdir'):
            launch_desktop(temporary)
            self.assertEqual(make_server.call_args.args[:2], ('127.0.0.1', 0))
            browser.assert_called_once_with('http://127.0.0.1:8099/?profile=approved')
            server.server_close.assert_called_once()
            client = app.test_client()
            for method, route in [('PATCH', '/api/profiles/approved'), ('DELETE', '/api/profiles/approved'),
                                  ('POST', '/api/profiles/approved/process')]:
                self.assertEqual(client.open(route, method=method).status_code, 403)
            self.assertNotEqual(client.post('/api/profiles/approved/copy').status_code, 403)
            self.assertNotEqual(client.patch('/api/profiles/personal').status_code, 403)
            self.assertEqual(client.get('/api/profiles').json['baseline_slug'], 'approved')

    def test_release_seeds_private_workspace_without_overwriting_user_changes(self):
        from desktop_release import prepare_workspace
        from os.path import relpath

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release_root = root / 'release'
            slug, version = 'approved-socket-report', 'a' * 32
            state = release_root / 'baseline' / 'Saved_Sessions' / 'profiles' / slug
            state.mkdir(parents=True)
            (state / 'manifest.json').write_text(json.dumps({'version': version}), encoding='utf-8-sig')
            config = release_root / 'baseline' / 'config'
            config.mkdir()
            (config / 'limits.json').write_text('{"limit": 105}')
            metadata = {'release_id': 'socket-v1', 'baseline_slug': slug, 'baseline_version': version}
            (release_root / 'release.json').write_text(json.dumps(metadata))
            workspace, actual = prepare_workspace(release_root, relpath(root / 'private'))
            self.assertTrue(workspace.is_absolute())
            self.assertEqual(actual, metadata)
            self.assertEqual((workspace / 'config' / 'limits.json').read_text(), '{"limit": 105}')
            (workspace / 'config' / 'limits.json').write_text('{"limit": 100}')
            again, _ = prepare_workspace(release_root, root / 'private')
            self.assertEqual(again, workspace)
            self.assertEqual((again / 'config' / 'limits.json').read_text(), '{"limit": 100}')
            self.assertEqual((config / 'limits.json').read_text(), '{"limit": 105}')
            metadata['release_id'] = 'socket-v2'
            (release_root / 'release.json').write_text(json.dumps(metadata))
            updated, _ = prepare_workspace(release_root, root / 'private')
            self.assertNotEqual(updated, workspace)
            self.assertEqual((updated / 'config' / 'limits.json').read_text(), '{"limit": 105}')
            deep_workspace, _ = prepare_workspace(release_root, root / ('private-' + 'long' * 50))
            try:
                self.assertEqual((deep_workspace / 'config' / 'limits.json').read_text(), '{"limit": 105}')
            finally:
                shutil.rmtree(deep_workspace.parent)

    def test_release_rejects_mismatched_baseline(self):
        from desktop_release import prepare_workspace

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / 'baseline' / 'Saved_Sessions' / 'profiles' / 'approved'
            state.mkdir(parents=True)
            (state / 'manifest.json').write_text(json.dumps({'version': 'b' * 32}))
            (root / 'release.json').write_text(json.dumps({
                'release_id': 'v1', 'baseline_slug': 'approved', 'baseline_version': 'a' * 32}))
            with self.assertRaisesRegex(ValueError, 'does not match'):
                prepare_workspace(root, root / 'private')
            self.assertFalse((root / 'private').exists())


class SharedDashboardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "repository"
        self.publish = Path(temporary.name) / "published"
        (self.root / "assets").mkdir(parents=True)
        (self.root / "assets" / "profile_portal.html").write_text(TEMPLATE, encoding="utf-8")
        (self.root / "config").mkdir()
        self.records = [
            {"TEST_NAME": "DTS_DIODE_S1", "COLUMN_NAMES": "sensor_a, sensor_b"},
            {"TEST_NAME": "DTS_S1", "COLUMN_NAMES": ["sensor_a", "sensor_b"]},
            {"TEST_NAME": "DTSMON_DIODE_S1", "COLUMN_NAMES": "sensor_a, sensor_b"},
            {"TEST_NAME": "DTS_MON_S1", "COLUMN_NAMES": "sensor_a, sensor_b"},
            {"TEST_NAME": "DTS-MON_S1", "COLUMN_NAMES": "sensor_a, sensor_b"},
            {"TEST_NAME": "OTHER_S1", "COLUMN_NAMES": "sensor_a, sensor_b", "CATEGORY": "Thermal Diode"},
            {"TEST_NAME": "DTS_OVERRIDE_S1", "COLUMN_NAMES": "sensor_a, sensor_b", "CATEGORY": "DTSMON"},
            {"TEST_NAME": "DTS_S2", "COLUMN_NAMES": "sensor_a, sensor_b"},
            {"TEST_NAME": "UNCLASSIFIED_S1", "COLUMN_NAMES": "sensor_a, sensor_b"},
            {"TEST_NAME": "DTS_ABSENT_S1", "COLUMN_NAMES": "sensor_a, sensor_b"},
        ]
        self.config_path = self.root / "config" / "thermal.json"
        self.config_path.write_text(json.dumps(self.records), encoding="utf-8")
        self.limits = {"P1": [{"test": "DTS", "LCL": 0, "UCL": 10}]}
        self.limit_path = self.root / "config" / "Product_Test_Limits.json"
        self.limit_path.write_text(json.dumps(self.limits), encoding="utf-8")
        self.app = shared.create_app(self.root, self.publish)
        self.app.testing = True
        self.app.config['AUTO_PROCESS_NEW_REPORTS'] = False
        self.client = self.app.test_client()
        self.calls = []
        self.exporter = patch.object(shared, "export_dashboard", side_effect=self.fake_export)
        self.export_mock = self.exporter.start()
        self.addCleanup(self.exporter.stop)
        self.fetcher = Mock(side_effect=AssertionError("Manual processing called a connector"))
        self.connector_patch = patch.object(profiles, "AQUA_TRACE_FETCHER", self.fetcher)
        self.connector_patch.start()
        self.addCleanup(self.connector_patch.stop)

    def test_aqua_create_starts_one_background_job_and_records_completion(self):
        self.app.config['AUTO_PROCESS_NEW_REPORTS'] = True
        with patch.object(shared.threading, 'Thread') as thread:
            result = self.create(source='aqua')
            slug = result['slug']
            self.assertEqual(result['job']['status'], 'queued')
            again = self.client.post('/api/profiles/' + slug + '/jobs', json={})
            self.assertEqual(again.status_code, 202)
            self.assertEqual(again.json['id'], result['job']['id'])
            thread.assert_called_once()
            self.write_input(slug, [('DTS_DIODE_S1', '10|20', 'u1', 'L1', 'Prog1', 'P1')])
            real_process = shared._process

            def process(root, publish, slug, pull, domain, progress=None):
                self.assertTrue(pull)
                progress('Retrieving 1 lot(s) from AQUA (attempt 1 of 3).')
                saved = self.client.get('/api/profiles/' + slug + '/jobs').json['job']
                self.assertIn('Retrieving 1 lot(s)', saved['message'])
                self.assertIn('elapsed_seconds', saved)
                return real_process(root, publish, slug, False, domain)

            with patch.object(shared, '_process', side_effect=process):
                thread.call_args.kwargs['target']()
            job = self.client.get('/api/profiles/' + slug + '/jobs').json['job']
            self.assertEqual(job['status'], 'completed')
            self.assertEqual(set(job['category_counts']), set(shared.CATEGORIES))
            self.assertIn('No matching data:', job['message'])
            self.assertTrue(job['finished_at'])

    def test_background_failure_preserves_snapshot_and_reports_error(self):
        slug = self.create()['slug']
        self.write_input(slug, [('DTS_DIODE_S1', '10|20', 'u1', 'L1', 'Prog1', 'P1')])
        previous = self.successful(slug)['snapshot']['version']
        with patch.object(shared.threading, 'Thread') as thread:
            self.client.post('/api/profiles/' + slug + '/jobs', json={'pull': False})
            with patch.object(shared, '_process', side_effect=ValueError('Retrieval unavailable')):
                thread.call_args.kwargs['target']()
        job = self.client.get('/api/profiles/' + slug + '/jobs').json['job']
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['message'], 'Retrieval unavailable')
        self.assertEqual(self.client.get('/api/profiles/' + slug).json['snapshot']['version'], previous)

    def test_named_dts_exact_list_preserves_category_specific_sensor_mappings(self):
        records = [{'TEST_NAME': 'NAMED_RESULT', 'COLUMN_NAMES': ['TDAU1', 'AON']}]
        (self.root / 'config' / 'DTS.json').write_text(json.dumps(records), encoding='utf-8')
        (self.root / 'config' / 'Thermal_Diode.json').write_text(json.dumps([
            {'TEST_NAME': 'NAMED_RESULT', 'COLUMN_NAMES': ['CPU1']}]), encoding='utf-8')
        config, _, _, files = shared._load_configuration(self.root)
        self.assertEqual(shared._configuration_metadata(config, files)['category_tests']['dts'],
                         ['NAMED_RESULT'])
        self.assertEqual(config['NAMED_RESULT']['CATEGORY_COLUMN_NAMES'],
                         {'dts': ['TDAU1', 'AON'], 'thermal_diode': ['CPU1']})
        self.assertEqual(config['NAMED_RESULT']['COLUMN_NAMES'], ['TDAU1', 'AON'])
        self.assertEqual(config['NAMED_RESULT']['RESULT_PARSER'], 'dts_reference')
        slug = self.create()['slug']
        self.write_input(slug, [('NAMED_RESULT', '90|95|999', 'u1', 'L1', 'Prog1', 'P1'),
                    ('NAMED_RESULT_EXTRA', '999', 'u1', 'L1', 'Prog1', 'P1')])
        result = self.successful(slug)
        calls = {call['report_kind']: call for call in self.calls}
        self.assertEqual({row['Measurement_Point'] for row in calls['dts']['rows']}, {'TDAU1', 'AON'})
        self.assertEqual({row['Measurement_Point'] for row in calls['thermal_diode']['rows']}, {'CPU1'})
        self.assertEqual(result['category_counts']['dts']['readings'], 2)
        self.assertEqual(result['category_counts']['thermal_diode']['readings'], 1)
        self.assertEqual(result['counts']['readings'], 2)
        self.assertIsNone(result['snapshot']['generated_dts_config'])
        self.assertEqual(calls['dts']['sensor_config']['NAMED_RESULT']['COLUMN_NAMES'], ['TDAU1', 'AON'])

    def test_dtsmon_discovery_preserves_first_seen_named_sensors_without_inventing_positions(self):
        name = 'IPC::FUN_MODULE::TEMP_DTS'
        config = {name: {'TEST_NAME': name, 'COLUMN_NAMES': ['Z', 'Absent'],
                         'CATEGORIES': ['dtsmon'], 'SOURCE_CONFIGS': ['DTSMON.json']}}
        original = json.dumps(config, sort_keys=True)
        folder = self.root / 'input'
        folder.mkdir()
        with (folder / 'readings.csv').open('w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['TEST_NAME', 'TEST_RESULT', 'PARAMETER_GROUP', 'TEST_RESULT_NUMERIC'])
            writer.writerows([(name, 'Z:91|B:92', 'STRING', ''),
                              (name, 'A:93|B:94', 'STRING', ''),
                              ('IPH::ARR_NEW::TEMP_DTS', 'C:95|A:96', 'STRING', ''),
                              ('IPC::ARR_POSITIONAL::TEMP_DTS', '91|92', 'STRING', ''),
                              ('IPC::ARR_NUMERIC::TEMP_DTS', 'Unused:999', 'PARAMETRIC', '91'),
                              ('IPH::ARR_REJECT::TEMP_DTS_1', 'A:97', 'STRING', '')])
        result = shared._discover_dtsmon_sensors(folder, config)
        self.assertEqual(set(result), {name, 'IPH::ARR_NEW::TEMP_DTS'})
        self.assertEqual(result[name]['COLUMN_NAMES'], ['Z', 'Absent', 'B', 'A'])
        self.assertEqual(result['IPH::ARR_NEW::TEMP_DTS']['COLUMN_NAMES'], ['C', 'A'])
        self.assertTrue(all(record['RESULT_PARSER'] == 'dts_reference' for record in result.values()))
        self.assertEqual(json.dumps(config, sort_keys=True), original)

    def test_dtsmon_patterns_preserve_config_and_allow_new_test_names(self):
        allowed = [f'{scope}::{kind}_MODULE::TEMP_DTS{suffix}'
                   for scope in ('IPC', 'IPH', 'IPG')
                   for kind in ('ARR', 'FUN', 'SCN')
                   for suffix in (('', '_1', '_2') if scope == 'IPC' else ('',))]
        rejected = ['IPC::PTH_DTS_MODULE::TEMP_DTS', 'IPH::FUN_MODULE::TEMP_DTS_1',
                    'IPG::SCN_MODULE::TEMP_DTS_2', 'IPC::ARR_MODULE::TEMP_DTS_3',
                    'IPC::ARR_MODULE::TEMP_DTS_EXTRA', 'OTHER::ARR_MODULE::TEMP_DTS',
                    'IPC::OTHER_MODULE::TEMP_DTS']
        config = {name: {'TEST_NAME': name, 'COLUMN_NAMES': ['configured_sensor'],
                         'CATEGORIES': ['dtsmon'], 'SOURCE_CONFIGS': ['DTSMON.json']}
                  for name in allowed + rejected}
        config['IPC::PTH_DTS_MODULE::CALIBRATION'] = {
            'COLUMN_NAMES': ['calibration_sensor'], 'CATEGORIES': ['dts']}
        config['THERMAL_DIODE'] = {'COLUMN_NAMES': ['diode_sensor'],
                                   'CATEGORIES': ['thermal_diode']}
        discovered = {name: ['observed_sensor'] for name in config}
        discovered['IPC::FUN_UNCONFIGURED::TEMP_DTS'] = ['unconfigured_sensor']
        grouped = shared._group_dts_tests(config, discovered)
        self.assertEqual({name for name, record in grouped.items()
                          if 'dtsmon' in record['CATEGORIES']},
                         set(allowed) | {'IPC::FUN_UNCONFIGURED::TEMP_DTS'})
        self.assertEqual(grouped[allowed[0]]['COLUMN_NAMES'], ['configured_sensor'])
        self.assertEqual(grouped['IPC::FUN_UNCONFIGURED::TEMP_DTS']['COLUMN_NAMES'],
                         ['unconfigured_sensor'])
        self.assertEqual(grouped['IPC::PTH_DTS_MODULE::CALIBRATION']['CATEGORIES'], ['dts'])
        self.assertEqual(grouped['THERMAL_DIODE']['CATEGORIES'], ['thermal_diode'])

    def test_dts_membership_wins_and_unconfigured_tests_are_excluded(self):
        original = {
            'IPC::DTS_LISTED': {'TEST_NAME': 'IPC::DTS_LISTED', 'COLUMN_NAMES': ['AON'],
                                'CATEGORIES': ['dts', 'dtsmon']},
            'OTHER': {'TEST_NAME': 'OTHER', 'COLUMN_NAMES': ['sensor'], 'CATEGORIES': []},
        }
        grouped = shared._group_dts_tests(original, {
            'IPC::DTS_LISTED': ['AON'], 'IPC::DTS_LISTED_SUFFIX': ['CCF0S0'],
            'OTHER': ['sensor'], 'IPC::NEW_DTS': ['Sensor_0'],
        })
        self.assertEqual(grouped['IPC::DTS_LISTED']['CATEGORIES'], ['dts'])
        self.assertNotIn('IPC::DTS_LISTED_SUFFIX', grouped)
        self.assertNotIn('IPC::NEW_DTS', grouped)
        self.assertEqual(grouped['OTHER']['CATEGORIES'], [])
        self.assertEqual(original['IPC::DTS_LISTED']['CATEGORIES'], ['dts', 'dtsmon'])

    def test_named_dts_processing_routes_only_allowed_dtsmon_patterns(self):
        maintained = json.dumps([{'TEST_NAME': 'PTH_DTS_MODULE::DTS_S1', 'COLUMN_NAMES': ['AON']}])
        (self.root / 'config' / 'DTS.json').write_text(maintained, encoding='utf-8')
        seed = json.dumps([
            {'TEST_NAME': 'IPC::ARR_MODULE::TEMP_DTS', 'COLUMN_NAMES': ['CCF0S1', 'CCF0S0']},
            {'TEST_NAME': 'IPC::FUN_MODULE::TEMP_DTS_2', 'COLUMN_NAMES': ['AON']},
            {'TEST_NAME': 'DTS_STALE', 'COLUMN_NAMES': ['AON']}])
        (self.root / 'config' / 'DTSMON.json').write_text(seed, encoding='utf-8')
        slug = self.create()['slug']
        self.write_input(slug, [
            ('PTH_DTS_MODULE::DTS_S1', 'AON:91', 'u1', 'L1', 'Prog1', 'P1'),
            ('IPC::ARR_MODULE::TEMP_DTS', 'CCF0S0:130|CCF0S1:95', 'u1', 'L1', 'Prog1', 'P1'),
            ('DTS_S1_SUFFIX', 'CCF0S0:130|CCF0S1:95', 'u1', 'L1', 'Prog1', 'P1'),
            ('IPC::SCN_UNCONFIGURED::TEMP_DTS', 'AON:99', 'u1', 'L1', 'Prog1', 'P1'),
            ('DTS_STALE', 'AON:99', 'u1', 'L1', 'Prog1', 'P1'),
            ('OTHER', '55', 'u1', 'L1', 'Prog1', 'P1'),
            ('DTS_OTHER_LOT', 'AON:999', 'u2', 'L2', 'Prog1', 'P1'),
        ])
        response = self.process(slug, category='dts')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()
        self.assertEqual(result['category_counts']['dts']['readings'], 1)
        self.assertEqual(result['category_counts']['dtsmon']['readings'], 3)
        self.assertEqual(result['category_tests']['dts'], ['PTH_DTS_MODULE::DTS_S1'])
        self.assertEqual(result['category_tests']['dtsmon'],
                 ['IPC::ARR_MODULE::TEMP_DTS', 'IPC::SCN_UNCONFIGURED::TEMP_DTS'])
        version = self.root / 'Saved_Sessions' / 'profiles' / slug / 'versions' / result['snapshot']['version']
        generated = json.loads((version / 'config' / 'DTSMON.json').read_text())
        self.assertEqual(generated, [{'TEST_NAME': 'IPC::ARR_MODULE::TEMP_DTS',
                                     'COLUMN_NAMES': ['CCF0S1', 'CCF0S0']},
                                    {'TEST_NAME': 'IPC::SCN_UNCONFIGURED::TEMP_DTS',
                                     'COLUMN_NAMES': ['AON']}])
        self.assertTrue(result['reports']['dtsmon'])
        self.write_input(slug, [('IPC::FUN_MODULE::TEMP_DTS_2', 'AON:80', 'u3', 'L1', 'Prog1', 'P1')])
        rerun = self.process(slug, category='dtsmon')
        self.assertEqual(rerun.status_code, 200, rerun.get_data(as_text=True))
        self.assertEqual(rerun.get_json()['category_tests']['dtsmon'], ['IPC::FUN_MODULE::TEMP_DTS_2'])
        self.assertIsNone(rerun.get_json()['reports']['dts'])
        self.assertEqual((self.root / 'config' / 'DTS.json').read_text(), maintained)
        self.assertEqual((self.root / 'config' / 'DTSMON.json').read_text(), seed)
        self.assertTrue((version / 'config' / 'DTSMON.json').exists())

    def fake_export(self, store, output_folder, product=None, source_note=None, plot_limit=5000,
                    filters=None, sensor_config=None, limits=None, report_kind='dts', limit_product=None,
                    report_product=None, per_socket=False):
        rows = store.plot_rows(filters, limit=plot_limit).to_dict("records")
        self.calls.append({"database": store.database, "filters": copy.deepcopy(filters),
                           "sensor_config": copy.deepcopy(sensor_config), "limits": copy.deepcopy(limits),
                           "rows": rows, "product": product, "source_note": source_note,
                           "report_kind": report_kind, "limit_product": limit_product, "report_product": report_product,
                           "per_socket": per_socket})
        destination = Path(output_folder) / "dashboard.html"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("<!doctype html><p>" + str(len(rows)) + " readings</p>", encoding="utf-8")
        return destination

    def test_workspace_info_identifies_portal_root(self):
        response = self.client.get('/api/workspace-info')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'application': 'Thermal Analysis Dashboard',
                                                'workspace': str(self.root.resolve())})

    def create(self, **overrides):
        body = {"owner": "Alice", "work_week": "WW46p4", "product": "P1",
                "test_program": "Prog1", "lots": ["L1"], "notes": "", "subflow_step": "S1",
                "source": "manual"}
        body.update(overrides)
        response = self.client.post("/api/profiles", json=body)
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()

    def write_input(self, slug, rows=None):
        folder = self.root / "input" / "profiles" / slug
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "readings.csv"
        if rows is None:
            rows = []
            for record in self.records:
                name = record["TEST_NAME"]
                if name == "DTS_ABSENT_S1":
                    continue
                rows.extend([
                    (name, "1|2", "good", "L1", "Prog1", "P1"),
                    (name, "100|200", "wrong-lot", "L2", "Prog1", "P1"),
                    (name, "100|200", "wrong-program", "L2", "Prog2", "P1"),
                    (name, "100|200", "wrong-product", "L1", "Prog1", "P2"),
                ])
            rows.append(("DTS_UNLISTED_S1", "999", "not-configured", "L1", "Prog1", "P1"))
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["TEST_NAME", "TEST_RESULT", "VISUAL_ID", "Lot", "Program_Name", "Product_Name"])
            writer.writerows(rows)
        return path

    def stage_trace_input(self, slug):
        from datetime import datetime, timezone
        from stage_trace_fallback import stage

        rows = [(record["TEST_NAME"], "1|2", "trace-unit", "L1", "Prog1", "P1")
                for record in self.records if record["TEST_NAME"] != "DTS_ABSENT_S1"]
        source = self.write_input(slug, rows)
        return stage(self.root, slug, [source], datetime.now(timezone.utc).isoformat())

    def process(self, slug, **body):
        return self.client.post(f"/api/profiles/{slug}/process", json={"pull": False, **body})

    def successful(self, slug):
        response = self.process(slug)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def portal_data(self, text):
        return json.loads(text.split('<script id="portal-data" type="application/json">', 1)[1].split("</script>", 1)[0])

    def test_real_template_has_two_tabs_and_static_mode(self):
        asset = Path(__file__).parent / "assets" / "profile_portal.html"
        shutil.copyfile(asset, self.root / "assets" / "profile_portal.html")
        live = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="library-tab"', live)
        self.assertIn('id="analysis-tab"', live)
        self.assertIn('data-category="dts"', live)
        self.assertIn('data-category="dtsmon"', live)
        self.assertIn('id="dtsmon-category"', live)
        self.assertIn('{pull,data_domain:domain}', live)
        self.assertNotIn('{pull,data_domain:domain,category}', live)
        self.assertIn('id="pull-fresh" type="checkbox"', live)
        self.assertIn('id="pull-fresh" type="checkbox" checked', live)
        self.assertIn('Boolean(selected&&selected.slug===data.baseline_slug&&data.baseline_readonly)', live)
        self.assertIn("if(changed)$('pull-fresh').checked=Boolean(selected&&selected.source!=='manual')", live)
        self.assertIn("populateForm({...profile,source:'aqua'})", live)
        self.assertIn("'/jobs',{pull,data_domain:domain}", live)
        self.assertIn("new Notification('Thermal Dashboard: '", live)
        self.assertNotIn('id="pull-btn"', live)
        self.assertLess(live.index('class="processing-options"'), live.index('id="category-view"'))
        self.assertNotIn('DTS and DTSMON are deferred', live)
        self.assertTrue(self.portal_data(live)["live"])
        destination = shared.publish_portal(self.root, self.publish)
        static = destination.read_text(encoding="utf-8")
        self.assertFalse(self.portal_data(static)["live"])
        self.assertNotIn("__PORTAL_DATA__", static)
        for document in (live, static):
            for label in ('Report Library', 'Report Analysis', 'Report library', 'Add report',
                          'Open report', 'Edit report', 'Copy report', 'Delete report',
                          'Create report', 'Work week / report name', 'No matching reports.'):
                self.assertIn(label, document)
            for label in ('Profile Library', 'Profile Analysis', 'Profile library', 'Add profile',
                          'Open profile', 'Edit profile', 'Copy profile', 'Delete profile',
                          'Create profile', 'Work week / profile name', 'No matching profiles.'):
                self.assertNotIn(label, document)
            self.assertIn('/api/profiles', document)
            self.assertIn('input/profiles/', document)
            self.assertIn('profiles', self.portal_data(document))

    def test_release_baseline_is_default_and_remains_processable_live(self):
        profile = self.create()
        self.write_input(profile['slug'])
        (self.root / 'release.json').write_text(json.dumps({
            'baseline_slug': profile['slug'], 'baseline_title': 'WW46p4 · Alice'}), encoding='utf-8')

        self.assertEqual(self.client.get('/api/profiles').json['baseline_slug'], profile['slug'])
        response = self.process(profile['slug'])
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def test_dts_generated_config_is_replaced_for_each_input(self):
        slug = self.create()['slug']
        original = self.config_path.read_bytes()
        self.write_input(slug, [('DTS_S1', 'AON:90|CCF0S0:95', 'unit1', 'L1', 'Prog1', 'P1')])
        response = self.process(slug, category='dts')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        first = response.get_json()
        first_version = self.root / 'Saved_Sessions' / 'profiles' / slug / 'versions' / first['snapshot']['version']
        generated = first_version / first['snapshot']['generated_dts_config']
        self.assertEqual(json.loads(generated.read_text()), [
            {'TEST_NAME': 'DTS_S1', 'COLUMN_NAMES': ['AON', 'CCF0S0']}])
        self.assertEqual(self.calls[-1]['report_kind'], 'dts')
        self.assertTrue(self.calls[-1]['per_socket'])
        self.write_input(slug, [('DTS_S1', 'C0S0:80', 'unit2', 'L1', 'Prog1', 'P1')])
        response = self.process(slug, category='dts')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        second = response.get_json()
        version = first_version.parent / second['snapshot']['version']
        self.assertEqual(json.loads((version / second['snapshot']['generated_dts_config']).read_text()), [
            {'TEST_NAME': 'DTS_S1', 'COLUMN_NAMES': ['C0S0']}])
        self.assertEqual(self.calls[-1]['sensor_config']['DTS_S1']['COLUMN_NAMES'], ['C0S0'])
        self.assertEqual(self.config_path.read_bytes(), original)
        self.assertTrue(generated.exists())
        self.assertEqual(list(first_version.parent.glob('.pending-*')), [])

    def test_product_names_follow_config_domain_and_updates(self):
        path = self.root / 'config' / 'Product_Name.txt'
        path.write_text('# Products\n@ CLASS\n1. HX28C\n6. S16C\n@ SORT\n1. C816_N2P\n', encoding='utf-8')
        library = self.client.get('/api/profiles').get_json()
        self.assertEqual(library['product_names'], {'CLASS': ['HX28C', 'S16C'], 'SORT': ['C816_N2P']})
        profile = self.create(product='HX28C', data_domain='CLASS')
        fields = {key: profile[key] for key in shared.PROFILE_FIELDS}
        fields['product'] = 'C816_N2P'
        self.assertEqual(self.client.put('/api/profiles/' + profile['slug'], json=fields).status_code, 422)
        self.assertEqual(self.client.post('/api/profiles', json=fields).status_code, 422)
        fields['data_domain'] = 'SORT'
        self.assertEqual(self.client.put('/api/profiles/' + profile['slug'], json=fields).status_code, 200)
        path.write_text('@ CLASS\n1. U8C\n@ SORT\n1. C816_BLLC\n', encoding='utf-8')
        self.assertEqual(self.client.get('/api/profiles').get_json()['product_names']['CLASS'], ['U8C'])
        path.write_text('@ INVALID\n1. X\n', encoding='utf-8')
        self.assertIn('product_names_error', self.client.get('/api/profiles').get_json())

    def test_profile_limit_product_is_independent_of_source_product(self):
        profile = self.create(product='nvlHX', data_domain='CLASS')
        path = self.write_input(profile['slug'])
        self.limit_path.write_text(json.dumps({
            '_product_aliases': {'nvlHX': 'NVL_Class_HX28C'},
            'NVL_Class_HX28C': [{'test': 'DTS_DIODE', 'LCL': 95, 'UCL': 105}]
        }), encoding='utf-8')
        saved = shared._load_profile(self.root, profile['slug'])
        fields = shared._profile_fields({key: saved[key] for key in shared.PROFILE_FIELDS if key in saved})
        fields['product'] = 'P1'
        result = shared._process_files(self.root, self.publish, profile['slug'], saved, fields,
                                       path.parent, False, category='thermal_diode')
        diode = next(call for call in self.calls if call['report_kind'] == 'thermal_diode')
        self.assertEqual(diode['limit_product'], 'NVL_Class_HX28C')
        self.assertEqual(diode['product'], 'P1')
        self.assertEqual(result['product'], 'nvlHX')
        manifest = json.loads((self.root / 'Saved_Sessions' / 'profiles' / profile['slug'] / 'manifest.json').read_text())
        snapshot = json.loads((self.root / 'Saved_Sessions' / 'profiles' / profile['slug'] /
                               'versions' / manifest['version'] / 'snapshot.json').read_text())
        self.assertEqual(snapshot['limit_product'], 'NVL_Class_HX28C')

    def test_diode_pools_lots_and_reloads_domain_product_limits(self):
        from test_category_reports import ReportParser

        (self.root / 'config' / 'Product_Name.txt').write_text('@ CLASS\n1. S8C\n', encoding='utf-8')
        profile = self.create(product='S8C', lots=['L1', 'L2'], analysis_categories=['thermal_diode'],
                              selected_tests={'thermal_diode': ['DTS_DIODE_S1']})
        source = self.write_input(profile['slug'], [])
        with source.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(['TEST_NAME', 'TEST_RESULT', 'VISUAL_ID', 'Lot', 'Program_Name', 'Product_Name', 'Socket'])
            for lot, raw_product in [('L1', 'raw-A'), ('L2', 'raw-B')]:
                writer.writerow(['DTS_DIODE_S1', '100', lot, lot, 'Prog1', raw_product, 'CLASSHOT'])
                writer.writerow(['DTS_DIODE_S1', '-10', lot, lot, 'Prog2', raw_product, 'CSM'])
        self.exporter.stop()
        previous = None
        for lower, upper in [(93, 107), (95, 105)]:
            limits = {'NVL_Class_S8C': [{'test': 'DTS_DIODE_S1', 'sockets': {
                'CLASSHOT': {'LCL': lower, 'UCL': upper}, 'CSM': {'LCL': -15, 'UCL': 5}}}]}
            self.limit_path.write_text(json.dumps(limits), encoding='utf-8')
            result = self.successful(profile['slug'])
            self.assertEqual(result['snapshot']['limit_product'], 'NVL_Class_S8C')
            folder = shared._state_folder(self.root, profile['slug']) / 'versions' / result['snapshot']['version']
            parsed = ReportParser(folder / 'thermal_diode' / 'dashboard.html')
            self.assertEqual(len(parsed.figures), 2)
            for figure in parsed.figures:
                metadata = figure['layout']['meta']
                self.assertEqual(metadata['count'], 2)
                expected = (lower, upper) if metadata['socket'] == 'CLASSHOT' else (-15, 5)
                self.assertEqual((metadata['lcl'], metadata['ucl']), expected)
                self.assertEqual(set(metadata['source_products']), {'raw-A', 'raw-B'})
            self.assertEqual(shared._read_json(folder / 'limits.json'), limits)
            if previous:
                self.assertEqual(shared._read_json(previous / 'limits.json')['NVL_Class_S8C'][0]['sockets']['CLASSHOT']['LCL'], 93)
                self.assertNotEqual(previous, folder)
            previous = folder

    def test_product_edit_regenerates_report_from_unchanged_sources(self):
        profile = self.create()
        self.write_input(profile['slug'])
        processed = self.client.post('/api/profiles/' + profile['slug'] + '/process',
                                     json={'pull': False, 'category': 'thermal_diode'}).get_json()
        previous_version = processed['snapshot']['version']
        (self.root / 'config' / 'Product_Name.txt').write_text('@ CLASS\n1. HX28C\n2. U8C\n', encoding='utf-8')
        fields = {key: profile[key] for key in shared.PROFILE_FIELDS}
        fields['product'] = 'HX28C'
        response = self.client.put('/api/profiles/' + profile['slug'], json=fields)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()
        self.assertTrue(result['report_updated'])
        self.assertEqual(result['snapshot']['report_product'], 'HX28C')
        self.assertEqual(result['snapshot']['filters']['Product_Name'], 'P1')
        self.assertEqual(result['counts'], processed['counts'])
        self.assertNotEqual(result['snapshot']['version'], previous_version)
        self.assertEqual(self.calls[-1]['report_product'], 'HX28C')
        self.assertTrue((self.root / 'Saved_Sessions' / 'profiles' / profile['slug'] /
                         'versions' / previous_version / 'snapshot.json').is_file())
        self.write_input(profile['slug'], rows=[('DTS_DIODE_S1', '500', 'changed', 'L1', 'Prog1', 'P1')])
        fields['product'] = 'U8C'
        response = self.client.put('/api/profiles/' + profile['slug'], json=fields)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()['report_updated'])
        self.assertEqual(response.get_json()['snapshot']['version'], result['snapshot']['version'])
        self.assertIn('Process', response.get_json()['warning'])

    def test_launcher_publishes_without_loading_main_dataset(self):
        from run_dashboard import main
        with patch.object(sys, "argv", ["run_dashboard.py", "--publish-profiles",
                                       "--publish-folder", str(self.publish)]), \
                patch.object(shared, "publish_portal", return_value=self.publish / "index.html") as publish:
            main()
        self.assertEqual(publish.call_args.args[1], self.publish)

    def test_launcher_serves_portal_on_loopback(self):
        from run_dashboard import main
        with patch.object(sys, "argv", ["run_dashboard.py", "--profile-portal", "--port", "8051",
                                       "--publish-folder", str(self.publish)]), \
                patch.object(shared, "create_app") as create:
            main()
        create.return_value.run.assert_called_once_with(host="127.0.0.1", port=8051,
                                                       debug=False, use_reloader=False)

    def test_duplicate_records_use_last_mapping_with_warning(self):
        records = [{"TEST_NAME": "PTH_DTS_MODULE::SHARED", "COLUMN_NAMES": "CPU0"},
                   {"TEST_NAME": "PTH_DTS_MODULE::SHARED", "COLUMN_NAMES": "TDAU0"}]
        (self.root / 'config' / 'DTS.json').write_text(json.dumps(records), encoding='utf-8')
        config, _, _, files = shared._load_configuration(self.root)
        self.assertEqual(config['PTH_DTS_MODULE::SHARED']['COLUMN_NAMES'], ['TDAU0'])
        self.assertEqual(config['PTH_DTS_MODULE::SHARED']['CATEGORIES'], ['dts'])
        info = shared._configuration_metadata(config, files)
        self.assertEqual(len(info['warnings']), 1)
        self.assertIn('last definition', info['warnings'][0])

    def test_thermal_diode_configuration_isolated_from_other_categories(self):
        diode = [{"TEST_NAME": "DIODE", "COLUMN_NAMES": "temperature"}]
        dts = [{"TEST_NAME": "DIODE", "COLUMN_NAMES": "sensor_a, sensor_b"}]
        (self.root / "config" / "Thermal_Diode.json").write_text(json.dumps(diode), encoding="utf-8")
        (self.root / "config" / "DTS.json").write_text(json.dumps(dts), encoding="utf-8")
        combined, _, _, combined_files = shared._load_configuration(self.root)
        self.assertEqual(combined['DIODE']['CATEGORIES'], ['dts', 'thermal_diode'])
        self.assertEqual(combined['DIODE']['CATEGORY_COLUMN_NAMES'],
                         {'dts': ['sensor_a', 'sensor_b'], 'thermal_diode': ['temperature']})
        self.assertEqual(shared._configuration_metadata(combined, combined_files)['warnings'], [])
        config, limits, raw, files = shared._load_configuration(self.root, "thermal_diode")
        self.assertEqual(config["DIODE"]["COLUMN_NAMES"], ["temperature"])
        self.assertEqual(config["DIODE"]["CATEGORIES"], ["thermal_diode"])
        self.assertEqual(set(raw), {"Thermal_Diode.json", "Product_Test_Limits.json"})
        self.assertEqual(limits, self.limits)
        self.assertEqual(shared._configuration_metadata(config, files)["category_tests"]["dts"], [])

    def test_thermal_diode_process_ignores_conflicting_dts_and_rejects_bad_category(self):
        diode = [{"TEST_NAME": "DIODE", "COLUMN_NAMES": "temperature"}]
        dts = [{"TEST_NAME": "DIODE", "COLUMN_NAMES": "sensor_a, sensor_b"}]
        (self.root / "config" / "Thermal_Diode.json").write_text(json.dumps(diode), encoding="utf-8")
        (self.root / "config" / "DTS.json").write_text(json.dumps(dts), encoding="utf-8")
        slug = self.create()["slug"]
        self.write_input(slug, [("DIODE", "91", "unit1", "L1", "Prog1", "P1")])
        response = self.process(slug, category="thermal_diode")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        snapshot = response.get_json()["snapshot"]
        self.assertEqual(snapshot["analysis_category"], "thermal_diode")
        self.assertEqual(snapshot["counts"]["readings"], 1)
        self.assertEqual([call["report_kind"] for call in self.calls], ["thermal_diode"])
        self.assertIsNone(snapshot["reports"]["dts"])
        library = self.client.get("/api/profiles?category=thermal_diode").get_json()
        self.assertNotIn("configuration_error", library)
        self.assertEqual(library["configuration"]["category_tests"]["thermal_diode"], ["DIODE"])
        self.assertEqual(self.process(slug, category="invalid").status_code, 400)
        self.assertEqual(self.client.get("/api/profiles?category=invalid").status_code, 400)

    def test_create_same_week_distinct_owners_and_uuid_ids(self):
        first = self.create()
        second = self.create(owner="Bob", lots=" L1, L2 ")
        self.assertNotEqual(first["slug"], second["slug"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["lots"], ["L1", "L2"])
        saved = list((self.root / "Profiles").glob("*.json"))
        self.assertEqual(len(saved), 2)
        self.assertEqual(json.loads((self.root / "Profiles" / f"{first['slug']}.json").read_text())["owner"], "Alice")
        data = self.client.get("/api/profiles").get_json()
        self.assertEqual(set(data), {"profiles", "service_url", "generated_at", "live", "configuration", "data_domains", "product_names", "owners", "owner_storage"})
        self.assertEqual(set(data['data_domains']), {'CLASS', 'SORT'})
        for domain, status in data['data_domains'].items():
            self.assertFalse(status['ready'])
            self.assertIn(f'AQUA {domain} parameter file is missing', status['error'])
        self.assertEqual({p["owner"] for p in data["profiles"]}, {"Alice", "Bob"})
        self.assertIsNone(data["service_url"])
        self.assertEqual(first["reports"], dict.fromkeys(shared.CATEGORIES))
        self.assertEqual(first["category_tests"], {category: [] for category in shared.CATEGORIES})
        self.assertEqual(first["counts"]["readings"], 0)
        self.assertIsNone(first["snapshot"])
        self.assertFalse((self.root / "Saved_Sessions").exists())
        self.assertFalse(list((self.root / "Profiles").glob("*.tmp")))

    def test_edit_preserves_identity_input_and_snapshot(self):
        original = self.create()
        slug = original["slug"]
        source = self.write_input(slug)
        source_bytes = source.read_bytes()
        processed = self.successful(slug)
        fields = {key: original[key] for key in shared.PROFILE_FIELDS}
        fields.update(owner="Bob", work_week="2026-WW47", lots="L2, L3", product="P2",
                      test_program="Prog2", notes="Updated notes", subflow_step="SORT",
                      source="aqua", data_domain="SORT")
        response = self.client.put(f"/api/profiles/{slug}", json=fields)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        updated = response.get_json()
        for key in ("slug", "id", "created"):
            self.assertEqual(updated[key], original[key])
        self.assertNotEqual(updated["updated"], original["updated"])
        for key, value in fields.items():
            self.assertEqual(updated[key], ["L2", "L3"] if key == "lots" else value)
        for key in ("snapshot", "reports", "counts"):
            self.assertEqual(updated[key], processed[key])
        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertEqual(len(list((self.root / "Profiles").glob("*.json"))), 1)
        self.assertEqual(self.client.get(f"/api/profiles/{slug}").get_json(), updated)
        published = self.portal_data((self.publish / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(published["profiles"][0]["owner"], "Bob")
        self.assertEqual(published["profiles"][0]["snapshot"], processed["snapshot"])
        self.fetcher.assert_not_called()

    def test_edit_rejects_invalid_missing_cross_origin_and_locked_requests(self):
        original = self.create()
        slug = original["slug"]
        path = self.root / "Profiles" / f"{slug}.json"
        saved = path.read_bytes()
        fields = {key: original[key] for key in shared.PROFILE_FIELDS}
        for changes in ({"owner": ""}, {"lots": [], "test_program": ""},
                        {"slug": "other"}, {"id": "other"}, {"data_domain": "invalid"}):
            with self.subTest(changes=changes):
                response = self.client.put(f"/api/profiles/{slug}", json={**fields, **changes})
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.put("/api/profiles/absent", json=fields).status_code, 404)
        self.assertEqual(self.client.put(f"/api/profiles/{slug}", json=fields,
                                        headers={"Origin": "https://evil.example"}).status_code, 403)
        with shared._locked(self.root, self.publish):
            self.assertEqual(self.client.put(f"/api/profiles/{slug}", json=fields).status_code, 409)
        self.assertEqual(path.read_bytes(), saved)

    def test_edit_publication_failure_keeps_saved_changes(self):
        original = self.create()
        fields = {key: original[key] for key in shared.PROFILE_FIELDS}
        fields["notes"] = "Saved despite publication failure"
        with patch.object(shared, "_publish_portal_locked", side_effect=OSError("Unavailable")):
            response = self.client.put(f"/api/profiles/{original['slug']}", json=fields)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Report updated", response.get_json()["warning"])
        self.assertEqual(shared._load_profile(self.root, original["slug"])["notes"], fields["notes"])

    def test_delete_archives_profile_and_preserves_data(self):
        original = self.create()
        other = self.create(owner="Bob")
        slug = original["slug"]
        source = self.write_input(slug)
        source_bytes = source.read_bytes()
        snapshot = self.successful(slug)["snapshot"]
        path = self.root / "Profiles" / f"{slug}.json"
        saved = path.read_bytes()
        response = self.client.delete(f"/api/profiles/{slug}")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json(), {"deleted": slug})
        self.assertFalse(path.exists())
        self.assertEqual(self.client.get(f"/api/profiles/{slug}").status_code, 404)
        archives = list((self.root / "Profiles" / "archive").glob(f"{slug}-*.json"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), saved)
        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertEqual(shared._snapshot(self.root, slug), snapshot)
        self.assertEqual([profile["slug"] for profile in self.client.get("/api/profiles").get_json()["profiles"]],
                         [other["slug"]])
        published = self.portal_data((self.publish / "index.html").read_text(encoding="utf-8"))
        self.assertEqual([profile["slug"] for profile in published["profiles"]], [other["slug"]])

    def test_delete_rejects_invalid_missing_cross_origin_and_locked_requests(self):
        slug = self.create()["slug"]
        path = self.root / "Profiles" / f"{slug}.json"
        saved = path.read_bytes()
        self.assertEqual(self.client.delete("/api/profiles/absent").status_code, 404)
        self.assertEqual(self.client.delete("/api/profiles/bad.name").status_code, 400)
        self.assertEqual(self.client.delete(f"/api/profiles/{slug}", json={"unexpected": True}).status_code, 400)
        self.assertEqual(self.client.delete(f"/api/profiles/{slug}",
                                           headers={"Origin": "https://evil.example"}).status_code, 403)
        with shared._locked(self.root, self.publish):
            self.assertEqual(self.client.delete(f"/api/profiles/{slug}").status_code, 409)
        self.assertEqual(path.read_bytes(), saved)

    def test_delete_publication_failure_keeps_profile_archived(self):
        slug = self.create()["slug"]
        with patch.object(shared, "_publish_portal_locked", side_effect=OSError("Unavailable")):
            response = self.client.delete(f"/api/profiles/{slug}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Report deleted", response.get_json()["warning"])
        self.assertEqual(self.client.get(f"/api/profiles/{slug}").status_code, 404)

    def test_lot_or_program_and_source_options(self):
        self.assertEqual(self.create(lots=[], source="aqua")["source"], "aqua")
        self.assertEqual(self.create(test_program="", source="trace")["source"], "trace")
        self.fetcher.assert_not_called()

    def test_new_profile_defaults_to_aqua(self):
        fields = {"owner": "Alice", "work_week": "WW46p5", "product": "P1",
                  "lots": ["L1"], "test_program": "", "subflow_step": ""}
        profile = self.client.post("/api/profiles", json=fields).get_json()
        self.assertEqual(profile["source"], "aqua")

    def test_selected_category_tests_and_lot_priority(self):
        profile = self.create(analysis_categories=['dts'], selected_tests={'dts': ['DTS_S1']})
        self.write_input(profile['slug'], [
            ('DTS_S1', '7', 'first', 'L1', 'Prog1', 'P1'),
            ('DTS_S1', '9', 'second', 'L1', 'OtherProgram', 'P1'),
            ('DTS_S2', '99', 'excluded', 'L1', 'Prog1', 'P1')])
        result = self.successful(profile['slug'])
        self.assertEqual(result['counts']['readings'], 2)
        self.assertEqual(result['available_tests']['dts'], ['DTS_S1'])
        self.assertIsNone(result['reports']['thermal_diode'])
        self.assertIsNone(result['reports']['dtsmon'])
        self.assertNotIn('Program_Name', result['snapshot']['filters'])

    def test_program_only_wildcard_processing(self):
        profile = self.create(lots=[], test_program='Prog*', analysis_categories=['dts'])
        self.write_input(profile['slug'], [
            ('DTS_S1', '7', 'first', 'L1', 'Prog1', 'P1'),
            ('DTS_S1', '9', 'second', 'L2', 'Prog2', 'P1'),
            ('DTS_S1', '99', 'excluded', 'L1', 'Other', 'P1')])
        result = self.successful(profile['slug'])
        self.assertEqual(result['counts']['readings'], 2)
        self.assertEqual(set(result['snapshot']['filters']['Program_Name']), {'Prog1', 'Prog2'})

    def test_owner_folder_contains_report_and_source_snapshot(self):
        reports = self.root / 'shared owners'
        (self.root / 'config' / 'owner_storage.json').write_text(json.dumps({'reports_root': str(reports)}))
        response = self.client.post('/api/owners', json={'name': 'Sarah'})
        self.assertEqual(response.status_code, 201)
        for name in ('../escape', 'CON', 'LPT1', 'Alice/other'):
            self.assertEqual(self.client.post('/api/owners', json={'name': name}).status_code, 400)
        profile = self.create(owner='Alan')
        self.write_input(profile['slug'])
        result = self.successful(profile['slug'])
        folder = reports / 'Alan' / profile['slug']
        self.assertTrue((folder / 'profile.json').is_file())
        version = folder / 'versions' / result['snapshot']['version']
        self.assertTrue((version / 'input' / 'readings.csv').is_file())
        self.assertTrue((version / 'snapshot.json').is_file())
        self.assertEqual(self.client.get('/api/profiles').get_json()['owners'], ['Alan', 'Sarah'])
        with patch.object(shared, '_save_owner_report', side_effect=OSError('share unavailable')):
            response = self.process(profile['slug'])
        self.assertEqual(response.status_code, 503)
        self.assertEqual(shared._snapshot(self.root, profile['slug'])['version'], result['snapshot']['version'])

    def test_dtsmon_fetch_patterns_do_not_repeat_configured_tests(self):
        fields = shared._profile_fields({'owner': 'Alan', 'work_week': 'WW1', 'lots': ['L1'],
                                         'analysis_categories': ['dtsmon']})
        tests = shared._fetch_tests({'IPC::ARR_CORE::TEMP_DTS': {}, 'IPC::FUN_CPU::TEMP_DTS_1': {}}, fields)
        self.assertEqual(tests, set(shared.DTSMON_PATTERNS))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows extended paths')
    def test_owner_snapshot_supports_long_storage_paths(self):
        reports = self.root / ('shared-owners-' + 'long' * 24)
        (self.root / 'config' / 'owner_storage.json').write_text(json.dumps({'reports_root': str(reports)}))
        profile = self.create(owner='Ronaldo', work_week='2026-WW47p2_S8C-MTR')
        version = 'a' * 32
        source = shared._state_folder(self.root, profile['slug']) / 'versions' / version
        files = ['parameters/NVL_Aqua_Class_Automation_Parameters.txt',
                 'thermal_diode/diode-trace-dd200794a34e4afbbc50d9518f35e619.csv']
        for name in files:
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'preserved report data')
        inputs = self.write_input(profile['slug']).parent
        snapshot = {'version': version}
        destination = shared._save_owner_report(self.root, profile, snapshot, inputs)
        self.assertFalse(destination.startswith('\\\\?\\'))
        target = shared._extended_storage_path(destination)
        self.assertGreater(len(str(Path(destination) / 'versions' / version / files[0])), 260)
        for name in files:
            self.assertEqual((target / 'versions' / version / name).read_bytes(), b'preserved report data')
        self.assertTrue((target / 'versions' / version / 'input' / 'readings.csv').is_file())
        self.assertEqual(shared._read_json(target / 'manifest.json'), snapshot)
        self.assertFalse(list((target / 'versions').glob('.pending-*')))
        with patch.object(shared.shutil, 'copytree', side_effect=OSError('copy interrupted')):
            with self.assertRaises(OSError):
                shared._save_owner_report(self.root, profile, {'version': 'b' * 32})
        self.assertEqual(shared._read_json(target / 'manifest.json'), snapshot)
        self.assertFalse(list((target / 'versions').glob('.pending-*')))
        shutil.rmtree(shared._extended_storage_path(reports))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows extended paths')
    def test_extended_storage_path_handles_unc_and_existing_prefix(self):
        unc = Path(r'\\server\share\reports\Ronaldo')
        extended = shared._extended_storage_path(unc)
        self.assertEqual(str(extended), r'\\?\UNC\server\share\reports\Ronaldo')
        self.assertEqual(shared._extended_storage_path(extended), extended)

    def test_selected_subflow_uses_observed_socket_metadata(self):
        profile = self.create(subflows=['CSM'], analysis_categories=['dts'])
        path = self.write_input(profile['slug'], [])
        with path.open('w', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['TEST_NAME', 'TEST_RESULT', 'VISUAL_ID', 'Lot', 'Program_Name', 'Product_Name', 'OPERGROUP'])
            writer.writerows([['DTS_S1', '7', 'one', 'L1', 'Prog2', 'P1', '6248_CSM'],
                              ['DTS_S1', '99', 'two', 'L1', 'Prog1', 'P1', '6248_CLASSHOT']])
        result = self.successful(profile['slug'])
        self.assertEqual(result['counts']['readings'], 1)
        self.assertEqual(result['snapshot']['filters']['Socket'], ['CSM'])

    def test_report_locks_allow_other_reports_and_creation(self):
        first = self.create()
        second = self.create(owner='Bob')
        self.write_input(second['slug'])
        with shared._file_lock(shared._state_folder(self.root, first['slug']) / '.processing.lock'):
            self.assertEqual(self.process(first['slug']).status_code, 409)
            self.assertEqual(self.client.delete('/api/profiles/' + first['slug']).status_code, 409)
            self.assertEqual(self.successful(second['slug'])['counts']['readings'], 16)
            self.assertEqual(self.create(owner='Sarah')['owner'], 'Sarah')

    def test_create_defaults_class_and_validates_explicit_data_domains(self):
        default = self.create()
        self.assertEqual(default['data_domain'], 'CLASS')
        for domain in ('CLASS', 'SORT'):
            with self.subTest(domain=domain):
                profile = self.create(data_domain=domain, subflow_step='CLASSHOT', source='aqua')
                self.assertEqual(profile['data_domain'], domain)
                self.assertEqual(profile['source'], 'aqua')
                self.assertEqual(profile['subflow_step'], 'CLASSHOT')
                path = self.root / 'Profiles' / f"{profile['slug']}.json"
                self.assertEqual(json.loads(path.read_text())['data_domain'], domain)
                self.assertEqual(self.client.get(f"/api/profiles/{profile['slug']}").get_json()['data_domain'], domain)
        saved = {path.name: path.read_bytes() for path in (self.root / 'Profiles').iterdir()}
        for domain in (None, '', 'class', 'sort', ' SORT ', 'TRACE', 0, True, [], {}):
            with self.subTest(invalid_domain=domain):
                response = self.client.post('/api/profiles', json={
                    'owner': 'Alice', 'work_week': 'W1', 'lots': ['L1'], 'data_domain': domain})
                self.assertEqual(response.status_code, 400)
                self.assertIn('data_domain must be CLASS or SORT', response.get_json()['error'])
        self.assertEqual({path.name: path.read_bytes() for path in (self.root / 'Profiles').iterdir()}, saved)
        self.export_mock.assert_not_called()
        self.fetcher.assert_not_called()

    def test_process_domain_override_is_snapshotted_separately_from_profile_labels(self):
        for saved_domain, selected_domain in (('CLASS', 'SORT'), ('SORT', 'CLASS')):
            with self.subTest(saved_domain=saved_domain, selected_domain=selected_domain):
                slug = self.create(data_domain=saved_domain, subflow_step='CLASSHOT', source='trace')['slug']
                self.write_input(slug)
                profile_path = self.root / 'Profiles' / f'{slug}.json'
                saved = profile_path.read_bytes()
                first = self.successful(slug)['snapshot']
                self.assertEqual(first['data_domain'], saved_domain)
                self.calls.clear()
                response = self.process(slug, data_domain=selected_domain)
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                processed = response.get_json()
                snapshot = processed['snapshot']
                self.assertEqual(snapshot['data_domain'], selected_domain)
                self.assertEqual(snapshot['profile']['data_domain'], selected_domain)
                self.assertEqual(snapshot['profile']['source'], 'trace')
                self.assertEqual(snapshot['profile']['subflow_step'], 'CLASSHOT')
                self.assertEqual(processed['data_domain'], saved_domain)
                self.assertEqual(profile_path.read_bytes(), saved)
                self.assertIsNone(snapshot['retrieval'])
                self.assertIsNone(snapshot['parameter_file'])
                self.assertEqual(snapshot['filters'], first['filters'])
                self.assertEqual(processed['counts']['readings'], 16)
                self.assertNotEqual(snapshot['version'], first['version'])
                self.assertEqual([call['report_kind'] for call in self.calls], list(shared.CATEGORIES))
                for call in self.calls:
                    self.assertIn(f" | {selected_domain} | {call['report_kind']} | ", call['source_note'])
                    self.assertNotIn('data_domain', call['filters'])
                    self.assertEqual(call['per_socket'],
                                     selected_domain == 'CLASS')
                state = self.root / 'Saved_Sessions' / 'profiles' / slug
                for folder in (state / 'versions' / snapshot['version'],
                               self.publish / 'reports' / slug / snapshot['version']):
                    self.assertEqual(json.loads((folder / 'snapshot.json').read_text()), snapshot)
                static = self.portal_data((self.publish / 'index.html').read_text(encoding='utf-8'))
                published = next(profile for profile in static['profiles'] if profile['slug'] == slug)
                self.assertEqual(published['snapshot'], snapshot)
        self.fetcher.assert_not_called()

    def test_invalid_fields_and_slugs(self):
        base = {"owner": "Alice", "work_week": "WW46p4", "lots": ["L1"]}
        invalid = [
            {"owner": " "}, {"owner": 123}, {"owner": "x" * 121}, {"owner": "a\x00b"},
            {"work_week": ""}, {"work_week": "../week"}, {"work_week": "W" * 65},
            {"lots": []}, {"lots": ["L1", "L1"]}, {"lots": [""]}, {"lots": [42]},
            {"lots": None}, {"lots": ["L" + str(n) for n in range(101)]},
            {"lots": ["x" * 129]}, {"lots": "x" * 16001}, {"notes": "x" * 4001},
            {"test_program": False}, {"product": {}}, {"subflow_step": "x" * 257},
            {"source": "unknown"}, {"source": {}}, {"slug": "../../evil"}, {"unexpected": 1},
        ]
        for changes in invalid:
            with self.subTest(changes=str(changes)[:100]):
                response = self.client.post("/api/profiles", json={**base, **changes})
                self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
        for slug in ("..", "bad.name", "CON", "foo%5Cbar", "bad:slug", "x" * 121):
            with self.subTest(slug=slug):
                self.assertIn(self.client.get(f"/api/profiles/{slug}").status_code, (400, 404))
                self.assertIn(self.process(slug).status_code, (400, 404))
        self.assertEqual(self.client.get("/api/profiles/absent").status_code, 404)
        self.assertFalse((self.root / "Profiles").exists())

    def test_duplicate_malformed_oversized_and_non_json_bodies(self):
        for body in ('{"owner":"Alice","owner":"Bob","work_week":"W1","lots":["L1"]}',
                     '{"owner":NaN}', '[1,2]', 'null', 'true', '{bad',
                     '{"notes":{"a":1,"a":2}}'):
            with self.subTest(body=body):
                response = self.client.post("/api/profiles", data=body, content_type="application/json")
                self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/profiles", data="{}", content_type="text/plain")
        self.assertEqual(response.status_code, 415)
        response = self.client.post("/api/profiles", data=" " * (shared.MAX_BODY + 1), content_type="application/json")
        self.assertEqual(response.status_code, 413)
        self.assertFalse((self.root / "Profiles").exists())

    def test_same_origin_and_cross_site_protection(self):
        body = {"owner": "Alice", "work_week": "W1", "lots": ["L1"]}
        for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"},
                        {"Origin": "http://localhost:81"}, {"Origin": "http://localhost.evil"},
                        {"Origin": "http://localhost/path"}, {"Sec-Fetch-Site": "cross-site"},
                        {"Sec-Fetch-Site": "same-site", "Origin": "http://localhost"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.client.post("/api/profiles", json=body, headers=headers).status_code, 403)
        response = self.client.post("/api/profiles", json=body,
                                    headers={"Origin": "http://localhost:80", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(response.status_code, 201)
        slug = response.get_json()["slug"]
        self.assertEqual(self.client.post(f"/api/profiles/{slug}/process", json={"pull": False},
                                         headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)
        self.assertEqual(self.client.get("/api/profiles", headers={"Host": "evil.example"}).status_code, 400)

    def test_processing_filters_categories_snapshots_and_cache_isolation(self):
        first = self.create()
        second = self.create(owner="Bob")
        path = self.write_input(first["slug"])
        self.write_input(second["slug"], [("DTS_S1", "7", "bob-only", "L1", "Prog1", "P1")])
        processed = self.successful(first["slug"])
        expected = {"thermal_diode": ["DTS_DIODE_S1", "OTHER_S1"], "dts": ["DTS_ABSENT_S1", "DTS_S1", "DTS_S2"],
                    "dtsmon": ["DTS-MON_S1", "DTSMON_DIODE_S1", "DTS_MON_S1", "DTS_OVERRIDE_S1"]}
        available = dict(expected, dts=["DTS_S1", "DTS_S2"])
        self.assertEqual(processed["category_tests"], expected)
        self.assertEqual(processed["available_tests"], available)
        self.assertEqual(processed["counts"]["readings"], 16)
        self.assertEqual(processed["counts"]["tests"], 8)
        self.assertEqual(len(self.calls), 3)
        for call, category in zip(self.calls, shared.CATEGORIES):
            self.assertEqual(call['report_kind'], category)
            self.assertEqual(call["filters"], {"Lot": ["L1"],
                                               "Product_Name": "P1", "Test": available[category]})
            self.assertEqual({row["Unit_ID"] for row in call["rows"]}, {"good"})
            self.assertEqual(call["limits"], self.limits)
            self.assertEqual(call["sensor_config"]["DTS_DIODE_S1"]["COLUMN_NAMES"], ["sensor_a", "sensor_b"])
            self.assertEqual(call["database"], self.root / "Saved_Sessions" / "profiles" / f"{first['slug']}.duckdb")
        snapshot = processed["snapshot"]
        self.assertEqual(snapshot["sources"][0]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(snapshot["profile"]["owner"], "Alice")
        self.assertEqual(snapshot["profile"]["subflow_step"], "S1")
        self.assertEqual(snapshot["plot_limit"], 5000)
        folder = self.root / "Saved_Sessions" / "profiles" / first["slug"] / "versions" / snapshot["version"]
        self.assertEqual((folder / "config" / "thermal.json").read_bytes(), self.config_path.read_bytes())
        self.assertEqual(json.loads((folder / "limits.json").read_text()), self.limits)
        self.assertEqual(len(processed["config_snapshot"]["files"]), 2)
        for report in processed["reports"].values():
            self.assertTrue(report.startswith("/reports/"))
            with self.client.get(report) as response:
                self.assertEqual(response.status_code, 200)
        bob = self.successful(second["slug"])
        self.assertEqual(bob["counts"]["readings"], 1)
        self.assertEqual({row["Unit_ID"] for row in self.calls[-1]["rows"]}, {"bob-only"})
        self.assertNotEqual(self.calls[0]["database"], self.calls[-1]["database"])
        self.assertIsNone(bob["reports"]["thermal_diode"])
        self.assertIsNone(bob["reports"]["dtsmon"])
        self.assertEqual(bob["category_tests"], expected)
        self.assertEqual(bob["available_tests"], {"thermal_diode": [], "dts": ["DTS_S1"], "dtsmon": []})
        self.assertEqual(bob["category_counts"]["thermal_diode"]["readings"], 0)
        self.assertEqual(self.client.get(f"/api/profiles/{first['slug']}").get_json()["snapshot"], snapshot)
        self.fetcher.assert_not_called()

    def write_named_configuration(self):
        records = [{"TEST_NAME": "IPC::ARR_SHARED::TEMP_DTS", "COLUMN_NAMES": "sensor_a, sensor_b",
                    "CATEGORY": "ignored filename override"}]
        for filename in ("Thermal_Diode.json", "DTS.json", "DTSMON.json"):
            selected = ([dict(records[0], TEST_NAME='IPC::PTH_DTS_MODULE::CALIBRATION')]
                        if filename == 'DTS.json' else records)
            (self.root / "config" / filename).write_text(json.dumps(selected), encoding="utf-8")
        return records

    def test_named_files_shared_memberships_processing_and_snapshot(self):
        self.write_named_configuration()
        # Neither malformed legacy content nor connector settings may be parsed or published.
        for filename in ("Thermal_DTS_test_config.json", "unrelated.json", "aqua_cli.json"):
            (self.root / "config" / filename).write_text("not JSON", encoding="utf-8")
        config, limits, raw_files, files = shared._load_configuration(self.root)
        expected_files = {"thermal_diode": ["Thermal_Diode.json"], "dts": ["DTS.json"],
                          "dtsmon": ["DTSMON.json"]}
        shared_name = 'IPC::ARR_SHARED::TEMP_DTS'
        expected_tests = {category: [shared_name] for category in shared.CATEGORIES}
        dts_name = 'IPC::PTH_DTS_MODULE::CALIBRATION'
        expected_tests['dts'] = [dts_name]
        self.assertEqual(set(config), {shared_name, dts_name})
        self.assertEqual(set(config[shared_name]["CATEGORIES"]), {'thermal_diode', 'dtsmon'})
        self.assertEqual(config[shared_name]["SOURCE_CONFIGS"], ["DTSMON.json", "Thermal_Diode.json"])
        self.assertEqual(limits, self.limits)
        self.assertEqual(set(raw_files), {"DTS.json", "DTSMON.json", "Thermal_Diode.json", "Product_Test_Limits.json"})
        self.assertEqual(next(item for item in files if item["path"].endswith("Product_Test_Limits.json"))["categories"], [])
        metadata = self.client.get("/api/profiles").get_json()
        self.assertEqual(metadata["configuration"], {"category_files": expected_files, "category_tests": expected_tests, "warnings": []})
        self.assertNotIn("COLUMN_NAMES", json.dumps(metadata))
        self.assertNotIn("configuration_error", metadata)
        self.assertFalse((self.root / "Saved_Sessions").exists())
        slug = self.create()["slug"]
        self.write_input(slug, [(shared_name, "1|2", "good", "L1", "Prog1", "P1"),
                                (dts_name, "1|2", "good", "L1", "Prog1", "P1"),
                                ("DTS_S1", "9|9", "legacy", "L1", "Prog1", "P1")])
        processed = self.successful(slug)
        routed_config = copy.deepcopy(config)
        self.assertEqual(processed["category_tests"], expected_tests)
        self.assertEqual(processed["available_tests"], expected_tests)
        self.assertEqual(processed["counts"]["readings"], 4)
        self.assertEqual(processed["counts"]["tests"], 2)
        self.assertEqual(processed["snapshot"]["filters"]["Test"], [shared_name, dts_name])
        self.assertEqual(processed["config_snapshot"]["category_files"], expected_files)
        self.assertTrue(processed['reports']['thermal_diode'])
        self.assertTrue(processed['reports']['dts'])
        self.assertTrue(processed['reports']['dtsmon'])
        self.assertEqual(len(self.calls), 3)
        for category, call in zip(shared.CATEGORIES, self.calls):
            self.assertEqual(call['report_kind'], category)
            self.assertEqual(call["filters"]["Test"], expected_tests[category])
            self.assertEqual({row["Unit_ID"] for row in call["rows"]}, {'good'})
            self.assertEqual(call["sensor_config"], routed_config)
            self.assertEqual(processed["category_counts"][category]["readings"], 2)
        version = self.root / "Saved_Sessions" / "profiles" / slug / "versions" / processed["snapshot"]["version"]
        self.assertEqual(json.loads((version / "sensor_config.json").read_text()), routed_config)
        self.assertEqual({path.name for path in (version / "config").iterdir()},
                 set(raw_files) | {'DTSMON.seed.json'})
        static = self.portal_data((self.publish / "index.html").read_text(encoding="utf-8"))
        diode_metadata = self.client.get("/api/profiles?category=thermal_diode").get_json()
        self.assertEqual(static["configuration"], diode_metadata["configuration"])
        self.assertEqual(static["profiles"][0]["snapshot"], processed["snapshot"])

    def test_any_named_file_disables_legacy_and_uses_filename_not_test_name(self):
        path = self.root / "config" / "DtS.JsOn"
        path.write_text(json.dumps([{"TEST_NAME": "PTH_DTS_MODULE::DIODE_DTSMON", "COLUMN_NAMES": "s",
                                     "CATEGORY": "thermal_diode"}]), encoding="utf-8")
        config, _, _, files = shared._load_configuration(self.root)
        self.assertEqual(set(config), {"PTH_DTS_MODULE::DIODE_DTSMON"})
        self.assertEqual(config["PTH_DTS_MODULE::DIODE_DTSMON"]["CATEGORIES"], ["dts"])
        self.assertEqual(shared._configuration_metadata(config, files)["category_files"],
                         {"thermal_diode": [], "dts": ["DtS.JsOn"], "dtsmon": []})
        path.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(shared.SharedDashboardError, "No configured tests"):
            shared._load_configuration(self.root)
        path.write_text("broken", encoding="utf-8")
        with self.assertRaisesRegex(shared.SharedDashboardError, "Invalid configuration"):
            shared._load_configuration(self.root)

    def test_named_sensor_mappings_are_independent_and_library_survives(self):
        records = self.write_named_configuration()
        slug = self.create()["slug"]
        name = records[0]['TEST_NAME']
        self.write_input(slug, [(name, '91|92', 'u1', 'L1', 'Prog1', 'P1')])
        for columns in (["sensor_b", "sensor_a"], ["sensor_a"], ["different", "sensor_b"]):
            with self.subTest(columns=columns):
                conflicting = [dict(records[0], COLUMN_NAMES=columns)]
                (self.root / "config" / "DTSMON.json").write_text(json.dumps(conflicting), encoding="utf-8")
                response = self.process(slug)
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                call = self.calls[-1]
                self.assertEqual(call['report_kind'], 'dtsmon')
                self.assertEqual(call['sensor_config'][name]['COLUMN_NAMES'], columns)
                self.assertEqual({row['Measurement_Point'] for row in call['rows']}, set(columns))
                library = self.client.get("/api/profiles")
                self.assertEqual(library.status_code, 200)
                self.assertNotIn("configuration_error", library.get_json())
                self.assertEqual(library.get_json()["profiles"][0]["slug"], slug)
                self.assertEqual(self.client.get("/").status_code, 200)
                destination = shared.publish_portal(self.root, self.publish)
                static = self.portal_data(destination.read_text(encoding="utf-8"))
                self.assertNotIn("configuration_error", static)
                self.assertEqual(static["configuration"]["category_tests"]["thermal_diode"],
                                 ["IPC::ARR_SHARED::TEMP_DTS"])
        self.assertEqual(self.export_mock.call_count, 6)

    def test_legacy_categories_sources_and_connector_exclusion(self):
        (self.root / "config" / "AQUA_CLI.JSON").write_text("not JSON", encoding="utf-8")
        duplicate = self.root / "config" / "Thermal_DTS_test_config.json"
        duplicate.write_text(json.dumps(self.records), encoding="utf-8")
        config, _, raw_files, files = shared._load_configuration(self.root)
        for record in self.records:
            name = record["TEST_NAME"]
            category = shared._category(name, record)
            self.assertEqual(config[name]["CATEGORIES"], [category] if category else [])
            self.assertEqual(config[name]["SOURCE_CONFIGS"], ["Thermal_DTS_test_config.json", "thermal.json"])
        self.assertNotIn("AQUA_CLI.JSON", raw_files)
        metadata = shared._configuration_metadata(config, files)
        for filenames in metadata["category_files"].values():
            self.assertEqual(filenames, ["Thermal_DTS_test_config.json", "thermal.json"])
        self.assertIn("OTHER_S1", metadata["category_tests"]["thermal_diode"])
        self.assertIn("DTS_OVERRIDE_S1", metadata["category_tests"]["dtsmon"])

    def test_process_categories_list_is_authoritative_with_legacy_fallback(self):
        config, limits, raw_files, files = shared._load_configuration(self.root)
        config["DTS_S1"]["CATEGORIES"] = ["dts", "dtsmon"]
        config["DTS_S2"].pop("CATEGORIES")
        config["DTS_DIODE_S1"]["CATEGORIES"] = []
        slug = self.create()["slug"]
        self.write_input(slug)
        with patch.object(shared, "_load_configuration", return_value=(config, limits, raw_files, files)):
            processed = self.successful(slug)
        self.assertIn("DTS_S1", processed["category_tests"]["dts"])
        self.assertIn("DTS_S1", processed["category_tests"]["dtsmon"])
        self.assertIn("DTS_S2", processed["category_tests"]["dts"])
        self.assertNotIn("DTS_DIODE_S1", processed["category_tests"]["thermal_diode"])

    def test_missing_configuration_does_not_break_library(self):
        shutil.rmtree(self.root / "config")
        library = self.client.get("/api/profiles")
        self.assertEqual(library.status_code, 200)
        self.assertIn("No configured tests", library.get_json()["configuration_error"])
        self.assertEqual(library.get_json()["configuration"], {
            "category_files": {key: [] for key in shared.CATEGORIES},
            "category_tests": {key: [] for key in shared.CATEGORIES}})

    def test_no_matching_readings_or_configuration_is_not_success(self):
        profile = self.create()
        slug = profile["slug"]
        self.write_input(slug, [("DTS_UNCONFIGURED", "1", "unit", "L1", "Prog1", "P1")])
        response = self.process(slug)
        self.assertEqual(response.status_code, 422)
        self.assertIn("No configured matching readings", response.get_json()["error"])
        self.assertEqual(self.client.get(f"/api/profiles/{slug}").get_json()["counts"]["readings"], 0)
        self.export_mock.assert_not_called()
        self.config_path.write_text("[]", encoding="utf-8")
        self.assertEqual(self.process(slug).status_code, 422)
        self.assertFalse((self.root / "Saved_Sessions" / "profiles" / slug / "manifest.json").exists())

    def test_no_input_and_invalid_configuration(self):
        profile = self.create()
        self.assertEqual(self.process(profile["slug"]).status_code, 422)
        self.write_input(profile["slug"])
        for records in ({"wrong": "shape"}, [{"TEST_NAME": "DTS", "COLUMN_NAMES": []}],
                        [{"TEST_NAME": "DTS", "COLUMN_NAMES": "S1", "CATEGORY": "unknown"}],
                        [{"TEST_NAME": "DTS", "COLUMN_NAMES": "S1"},
                         {"TEST_NAME": "DTS", "COLUMN_NAMES": "S2"}]):
            self.config_path.write_text(json.dumps(records), encoding="utf-8")
            self.assertEqual(self.process(profile["slug"]).status_code, 422)

    def test_pull_without_connector_never_falls_back_to_existing_files(self):
        profile = self.create(source="aqua")
        self.write_input(profile["slug"])
        with patch.object(profiles, "AQUA_TRACE_FETCHER", None):
            response = self.process(profile["slug"], pull=True)
        self.assertEqual(response.status_code, 503)
        self.assertIn("No Aqua/Trace connector", response.get_json()["error"])
        self.assertIsNone(self.client.get(f"/api/profiles/{profile['slug']}").get_json()["snapshot"])
        self.export_mock.assert_not_called()
        self.assertFalse((self.root / "Saved_Sessions" / "profiles" / f"{profile['slug']}.duckdb").exists())
        self.successful(profile["slug"])  # Manual processing is independent of the source label.
        self.fetcher.assert_not_called()

    def test_pull_callback_arguments_and_callback_failure(self):
        profile = self.create(source="trace")
        slug = profile["slug"]
        source = self.write_input(slug)
        destinations = []

        def fetch(lots, program, product, dest):
            self.assertEqual((lots, program, product), (["L1"], "", "P1"))
            self.assertNotEqual(dest, source.parent)
            self.assertEqual(list(dest.iterdir()), [])
            destinations.append(dest)
            shutil.copy2(source, dest / "fresh.csv")
            return [dest / "fresh.csv"]

        with patch.object(profiles, "AQUA_TRACE_FETCHER", side_effect=fetch) as connector:
            response = self.process(slug, pull=True)
            self.assertEqual(response.status_code, 200)
            connector.assert_called_once()
            first = response.get_json()
            self.assertIsNone(first['snapshot']['analysis_category'])
            self.assertTrue(all(first['reports'][category] for category in shared.CATEGORIES))
            self.assertEqual([call['report_kind'] for call in self.calls], list(shared.CATEGORIES))
            self.assertTrue(all(call['per_socket'] for call in self.calls))
            response = self.process(slug, pull=True)
            self.assertEqual(response.status_code, 200)
        self.assertNotEqual(*destinations)
        self.assertTrue(all(not path.exists() for path in destinations))
        snapshot = response.get_json()["snapshot"]
        with patch.object(profiles, "AQUA_TRACE_FETCHER", side_effect=RuntimeError("offline")):
            failed = self.process(slug, pull=True)
        self.assertEqual(failed.status_code, 502)
        self.assertEqual(self.client.get(f"/api/profiles/{slug}").get_json()["snapshot"], snapshot)

    def test_empty_connector_result_does_not_process_stale_files(self):
        slug = self.create(source="aqua")["slug"]
        self.write_input(slug)
        for result in (None, [], "not-a-file-list", [123], ["missing.csv"]):
            with self.subTest(result=result), patch.object(profiles, "AQUA_TRACE_FETCHER", return_value=result):
                self.assertEqual(self.process(slug, pull=True).status_code, 502)
        self.export_mock.assert_not_called()
        self.assertIsNone(self.client.get(f"/api/profiles/{slug}").get_json()["snapshot"])

    def test_aqua_retries_three_times_then_uses_matching_staged_trace_export(self):
        slug = self.create(source="aqua")["slug"]
        self.stage_trace_input(slug)
        with patch.object(profiles, "AQUA_TRACE_FETCHER", side_effect=RuntimeError("offline")) as fetcher:
            response = self.process(slug, pull=True)
        self.assertEqual(fetcher.call_count, 3)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        snapshot = response.get_json()["snapshot"]
        self.assertEqual(snapshot["retrieval"]["source"], "trace")
        self.assertEqual(snapshot["retrieval"]["fallback_reason"], "AQUA pull failed after three attempts")
        self.assertEqual(response.get_json()["counts"]["readings"], 16)

    def test_trace_source_uses_staged_export_without_calling_aqua(self):
        slug = self.create(source="trace")["slug"]
        self.stage_trace_input(slug)
        with patch.object(profiles, "AQUA_TRACE_FETCHER", None):
            response = self.process(slug, pull=True)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["snapshot"]["retrieval"]["fallback_reason"],
                         "TRACE source selected")

    def test_aqua_three_failures_then_live_trace_for_all_categories(self):
        slug = self.create(source='aqua')['slug']
        source = self.write_input(slug)
        (self.root / '.vscode').mkdir()
        (self.root / '.vscode' / 'mcp.json').write_text('{}')

        def trace(root, fields, destination, tests):
            self.assertEqual(fields['slug'], slug)
            self.assertIn('DTS_S1', tests)
            destination.mkdir(parents=True)
            shutil.copy2(source, destination / 'trace.csv')
            (destination / 'retrieval.json').write_text(json.dumps({'source': 'trace', 'transport': 'TRACE MCP'}))
            return [destination / 'trace.csv']

        with patch.object(profiles, 'AQUA_TRACE_FETCHER', side_effect=RuntimeError('offline')) as aqua, patch(
                'mcp_connector.fetch_trace', side_effect=trace) as fallback:
            response = self.process(slug, pull=True)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(aqua.call_count, 3)
        fallback.assert_called_once()
        self.assertTrue(all(response.json['reports'][category] for category in shared.CATEGORIES))
        self.assertEqual(response.json['snapshot']['retrieval']['source'], 'trace')

    def test_staged_trace_fallback_rejects_stale_or_mismatched_receipts(self):
        slug = self.create(source="aqua")["slug"]
        self.stage_trace_input(slug)
        source = self.root / "input" / "profiles" / slug / "readings.csv"
        receipt_path = self.root / "input" / "profiles" / slug / "trace_fallback" / "retrieval.json"
        mutations = (
            lambda receipt: receipt.update(profile=dict(receipt["profile"], lots=["OTHER"])),
            lambda receipt: receipt.update(retrieved_at="2020-01-01T00:00:00+00:00"))
        for mutation in mutations:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            mutation(receipt)
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            with patch.object(profiles, "AQUA_TRACE_FETCHER", side_effect=RuntimeError("offline")) as fetcher:
                response = self.process(slug, pull=True)
            self.assertEqual(fetcher.call_count, 3)
            self.assertEqual(response.status_code, 502)
            self.assertIn("Staged TRACE MCP export is invalid", response.get_json()["error"])
            self.assertIsNone(self.client.get(f"/api/profiles/{slug}").get_json()["snapshot"])
            self.stage_trace_input(slug)

    def test_trace_staging_rejects_rows_outside_report_filters(self):
        from datetime import datetime, timezone
        from stage_trace_fallback import stage

        slug = self.create(source="aqua")["slug"]
        source = self.write_input(slug, [("DTS_S1", "1|2", "unit", "OTHER", "Prog1", "P1")])
        with self.assertRaisesRegex(ValueError, "unexpected lot"):
            stage(self.root, slug, [source], datetime.now(timezone.utc).isoformat())

    def test_processing_body_requires_boolean_pull(self):
        slug = self.create()["slug"]
        for body in ({"pull": "false"}, {"pull": 0}, {"pull": None}, {"surprise": True}):
            self.assertEqual(self.client.post(f"/api/profiles/{slug}/process", json=body).status_code, 400)

    def test_invalid_process_domain_never_fetches_or_replaces_snapshot(self):
        slug = self.create(source='aqua')['slug']
        self.write_input(slug)
        original = self.successful(slug)['snapshot']
        state = self.root / 'Saved_Sessions' / 'profiles' / slug
        manifest = (state / 'manifest.json').read_bytes()
        index = (self.publish / 'index.html').read_bytes()
        self.export_mock.reset_mock()
        for domain in (None, '', 'class', 'sort', ' SORT ', 'TRACE', 0, True, [], {}):
            with self.subTest(domain=domain):
                response = self.process(slug, pull=True, data_domain=domain)
                self.assertEqual(response.status_code, 400)
                self.assertIn('data_domain CLASS or SORT', response.get_json()['error'])
                self.assertEqual(self.client.get(f'/api/profiles/{slug}').get_json()['snapshot'], original)
                self.assertEqual((state / 'manifest.json').read_bytes(), manifest)
                self.assertEqual((self.publish / 'index.html').read_bytes(), index)
        self.assertEqual({path.name for path in (state / 'versions').iterdir()}, {original['version']})
        self.fetcher.assert_not_called()
        self.export_mock.assert_not_called()

    def test_subflow_is_unverified_metadata_not_a_test_name_filter(self):
        slug = self.create(subflow_step="CLASSHOT")["slug"]
        self.write_input(slug)
        processed = self.successful(slug)
        note = ("Subflow/operation metadata not verified; manual exports must already be scoped appropriately. "
                "TRACE preflight not performed.")
        self.assertEqual(processed["validation_note"], note)
        self.assertEqual(processed["snapshot"]["validation_note"], note)
        self.assertEqual(processed["snapshot"]["profile"]["subflow_step"], "CLASSHOT")
        self.assertEqual(processed["counts"]["readings"], 16)
        self.assertTrue(all("CLASSHOT" not in name for names in processed["category_tests"].values() for name in names))
        self.assertTrue(all(note in call["source_note"] for call in self.calls))
        static = self.portal_data((self.publish / "index.html").read_text(encoding="utf-8"))["profiles"][0]
        self.assertEqual(static["validation_note"], note)
        self.assertEqual(static["available_tests"], processed["available_tests"])

    def test_runtime_devrev_product_mapping_is_frozen(self):
        import atlas_thermal_dashboard_optimized as dashboard

        product = dashboard.PRODUCT_MAPPING["8PF6CV"]
        slug = self.create(product=product)["slug"]
        path = self.write_input(slug, [])
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["TEST_NAME", "TEST_RESULT", "VISUAL_ID", "Lot", "Program_Name", "DevRevStep"])
            writer.writerows([("DTS_S1", "3", "existing", "L1", "Prog1", "8PF6CV-newstep"),
                              ("DTS_S1", "4", "runtime", "L1", "Prog1", "RUNTIME-step")])

        def mutate_mapping(*args, **kwargs):
            dashboard.PRODUCT_MAPPING.clear()
            return self.fake_export(*args, **kwargs)

        with patch.dict(dashboard.PRODUCT_MAPPING, {"RUNTIME": product}):
            expected = copy.deepcopy(dashboard.PRODUCT_MAPPING)
            with patch.object(shared, "export_dashboard", side_effect=mutate_mapping):
                processed = self.successful(slug)
        snapshot = processed["snapshot"]
        self.assertEqual(processed["counts"]["readings"], 2)
        self.assertEqual({row["Product_Name"] for row in self.calls[-1]["rows"]}, {product})
        self.assertEqual(snapshot["product_mapping"], expected)
        self.assertEqual(snapshot["config_snapshot"]["product_mapping"], "product_mapping.json")
        version = snapshot["version"]
        for folder in (self.root / "Saved_Sessions" / "profiles" / slug / "versions" / version,
                       self.publish / "reports" / slug / version):
            self.assertEqual(json.loads((folder / "product_mapping.json").read_text()), expected)
            self.assertEqual(json.loads((folder / "snapshot.json").read_text())["product_mapping"], expected)

    def test_create_and_process_automatically_publish_without_nested_locks(self):
        client = shared.create_app(self.root, self.publish, "http://localhost:8051").test_client()
        with patch.object(shared, "_locked", wraps=shared._locked) as locks:
            response = client.post("/api/profiles", json={"owner": "Alice", "work_week": "W1", "lots": ["L1"], "source": "manual"})
            self.assertEqual(response.status_code, 201)
            self.assertEqual(locks.call_count, 1)
        slug = response.get_json()["slug"]
        index = self.publish / "index.html"
        created = self.portal_data(index.read_text(encoding="utf-8"))
        self.assertEqual(created["service_url"], "http://localhost:8051")
        self.assertEqual(created["profiles"][0]["slug"], slug)
        self.assertIsNone(created["profiles"][0]["snapshot"])
        self.write_input(slug)
        with patch.object(shared, "_locked", wraps=shared._locked) as locks:
            response = client.post(f"/api/profiles/{slug}/process", json={"pull": False})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(locks.call_count, 1)
        published = self.portal_data(index.read_text(encoding="utf-8"))["profiles"][0]
        self.assertEqual(published["snapshot"], response.get_json()["snapshot"])
        for report in published["reports"].values():
            self.assertTrue((self.publish / report).is_file())

    def test_create_publication_failure_returns_created_profile_warning(self):
        self.create()
        index = self.publish / "index.html"
        before = index.read_bytes()
        with patch.object(shared, "_publish_portal_locked", side_effect=OSError("share unavailable")):
            created = self.create(owner="Bob")
        self.assertIn("Report created", created["warning"])
        self.assertIn("POST /api/publish", created["warning"])
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual(self.client.get(f"/api/profiles/{created['slug']}").status_code, 200)
        self.assertEqual(self.client.post("/api/publish").status_code, 200)
        self.assertEqual(len(self.portal_data(index.read_text(encoding="utf-8"))["profiles"]), 2)

    def test_postcommit_index_failure_returns_new_snapshot_and_warning(self):
        slug = self.create()["slug"]
        self.write_input(slug)
        original = self.successful(slug)
        index = self.publish / "index.html"
        before = index.read_bytes()
        atomic_bytes = shared._atomic_bytes

        def fail_index(path, content):
            if path == index:
                raise OSError("index unavailable")
            return atomic_bytes(path, content)

        with patch.object(shared, "_atomic_bytes", side_effect=fail_index):
            processed = self.successful(slug)
        self.assertIn("Snapshot committed", processed["warning"])
        self.assertNotIn("preserved", processed["warning"])
        self.assertNotEqual(processed["snapshot"]["version"], original["snapshot"]["version"])
        self.assertEqual(self.client.get(f"/api/profiles/{slug}").get_json()["snapshot"], processed["snapshot"])
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual(self.client.post("/api/publish", json={}).get_json(), {"published": True})
        self.assertEqual(self.portal_data(index.read_text(encoding="utf-8"))["profiles"][0]["snapshot"], processed["snapshot"])

    def test_explicit_publish_origin_body_and_lock_protection(self):
        for headers in ({"Origin": "null"}, {"Origin": "https://evil.example"},
                        {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.client.post("/api/publish", json={}, headers=headers).status_code, 403)
        self.assertFalse((self.publish / "index.html").exists())
        self.assertEqual(self.client.post("/api/publish", json={"pull": True}).status_code, 400)
        self.assertEqual(self.client.post("/api/publish", data="{}", content_type="text/plain").status_code, 415)
        with shared._file_lock(self.root / ".shared_dashboard.lock"):
            self.assertEqual(self.client.post("/api/publish", json={}).status_code, 409)
        response = self.client.post("/api/publish", json={}, headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue((self.publish / "index.html").is_file())

    def test_explicit_publish_failure_keeps_index_and_snapshot(self):
        slug = self.create()["slug"]
        self.write_input(slug)
        original = self.successful(slug)
        index = self.publish / "index.html"
        before = index.read_bytes()
        with patch.object(shared, "_copy_version", side_effect=OSError("share offline")):
            with self.assertLogs(self.app.logger, level="ERROR"):
                response = self.client.post("/api/publish", json={})
        self.assertEqual(response.status_code, 500)
        self.assertIn("committed snapshots are unchanged", response.get_json()["error"])
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual(self.client.get(f"/api/profiles/{slug}").get_json()["snapshot"], original["snapshot"])

    def test_manual_source_cannot_pull_or_change_successful_snapshot(self):
        slug = self.create()["slug"]
        self.write_input(slug)
        original = self.successful(slug)
        self.assertEqual(self.process(slug, pull=True).status_code, 400)
        self.fetcher.assert_not_called()
        self.assertEqual(self.client.get(f"/api/profiles/{slug}").get_json()["snapshot"], original["snapshot"])

    def test_pull_processes_only_returned_fresh_files(self):
        slug = self.create(source="aqua")["slug"]
        stale = self.write_input(slug)
        before = stale.read_bytes()

        def fetch(lots, program, product, dest):
            shutil.copy2(stale, dest / "not-returned.csv")
            nested = dest / "nested"
            nested.mkdir()
            (nested / "fresh.CSV").write_text(
                "TEST_NAME,TEST_RESULT,VISUAL_ID,Lot,Program_Name,Product_Name\n"
                "DTS_S1,7,fresh-only,L1,Prog1,P1\n", encoding="utf-8")
            return ["nested/fresh.CSV"]

        with patch.object(profiles, "AQUA_TRACE_FETCHER", side_effect=fetch):
            response = self.process(slug, pull=True)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        processed = response.get_json()
        self.assertEqual(processed["counts"]["readings"], 1)
        self.assertEqual(len(processed["snapshot"]["sources"]), 1)
        self.assertEqual({row["Unit_ID"] for row in self.calls[-1]["rows"]}, {"fresh-only"})
        self.assertEqual(stale.read_bytes(), before)
        self.assertFalse(list((self.root / "Saved_Sessions").rglob(".fetch-*")))

    def test_invalid_pull_files_preserve_successful_snapshot_and_index(self):
        slug = self.create(source="trace")["slug"]
        stale = self.write_input(slug)
        original = self.successful(slug)
        index = self.publish / "index.html"
        before = index.read_bytes()
        self.export_mock.reset_mock()
        for case in ("empty", "tuple", "unsupported", "legacy", "missing", "outside", "traversal", "directory", "duplicate", "temporary"):
            def fetch(lots, program, product, dest):
                shutil.copy2(stale, dest / "valid.csv")
                for name in ("unsupported.pdf", "legacy.xls", "~$temporary.csv"):
                    (dest / name).write_text("not supported", encoding="utf-8")
                return {"empty": [], "tuple": (dest / "valid.csv",),
                    "unsupported": ["valid.csv", "unsupported.pdf"], "legacy": ["legacy.xls"],
                        "missing": ["missing.csv"], "outside": [stale], "traversal": ["../escape.csv"],
                        "directory": [dest], "duplicate": ["valid.csv", dest / "valid.csv"],
                        "temporary": ["~$temporary.csv"]}[case]

            with self.subTest(case=case), patch.object(profiles, "AQUA_TRACE_FETCHER", side_effect=fetch):
                self.assertEqual(self.process(slug, pull=True).status_code, 502)
            self.assertEqual(self.client.get(f"/api/profiles/{slug}").get_json()["snapshot"], original["snapshot"])
            self.assertEqual(index.read_bytes(), before)
        self.export_mock.assert_not_called()
        self.assertFalse(list((self.root / "Saved_Sessions").rglob(".fetch-*")))

    def test_failed_generation_and_publication_preserve_previous_snapshot(self):
        slug = self.create()["slug"]
        self.write_input(slug)
        original = self.successful(slug)
        index = shared.publish_portal(self.root, self.publish)
        original_index = index.read_bytes()
        manifest = self.root / "Saved_Sessions" / "profiles" / slug / "manifest.json"
        original_manifest = manifest.read_bytes()
        self.config_path.write_text(json.dumps(self.records[:1]), encoding="utf-8")
        with patch.object(shared, "export_dashboard", side_effect=RuntimeError("export failure")):
            with self.assertLogs(self.app.logger, level="ERROR"):
                self.assertEqual(self.process(slug).status_code, 500)
        self.assertEqual(manifest.read_bytes(), original_manifest)
        with patch.object(shared, "_copy_version", side_effect=OSError("publication failure")):
            with self.assertLogs(self.app.logger, level="ERROR"):
                self.assertEqual(self.process(slug).status_code, 500)
        self.assertEqual(manifest.read_bytes(), original_manifest)
        self.assertEqual(index.read_bytes(), original_index)
        self.assertEqual(self.client.get(f"/api/profiles/{slug}").get_json()["snapshot"], original["snapshot"])
        for url in original["reports"].values():
            with self.client.get(url) as response:
                self.assertEqual(response.status_code, 200)
        self.assertFalse(list((self.root / "Saved_Sessions").rglob(".pending-*")))
        self.assertFalse((self.root / ".shared_dashboard.lock").exists())
        self.assertFalse((self.publish / ".shared_dashboard.lock").exists())

    def test_configuration_frozen_even_if_live_files_change_during_export(self):
        slug = self.create()["slug"]
        self.write_input(slug)
        original_config = self.config_path.read_bytes()
        original_limits = self.limit_path.read_bytes()

        def change_live_config(*args, **kwargs):
            self.config_path.write_text("[]", encoding="utf-8")
            self.limit_path.write_text("{}", encoding="utf-8")
            return self.fake_export(*args, **kwargs)

        with patch.object(shared, "export_dashboard", side_effect=change_live_config):
            processed = self.successful(slug)
        version = processed["snapshot"]["version"]
        frozen = self.root / "Saved_Sessions" / "profiles" / slug / "versions" / version / "config"
        self.assertEqual((frozen / "thermal.json").read_bytes(), original_config)
        self.assertEqual((frozen / "Product_Test_Limits.json").read_bytes(), original_limits)
        self.assertTrue(all(call["limits"] == self.limits for call in self.calls))

    def test_input_changes_during_export_preserve_previous_snapshot(self):
        slug = self.create()["slug"]
        path = self.write_input(slug)
        original = self.successful(slug)

        def change_source(*args, **kwargs):
            with path.open("a", encoding="utf-8") as stream:
                stream.write("DTS_S1,6,new,L1,Prog1,P1\n")
            return self.fake_export(*args, **kwargs)

        with patch.object(shared, "export_dashboard", side_effect=change_source):
            self.assertEqual(self.process(slug).status_code, 409)
        self.assertEqual(self.client.get(f"/api/profiles/{slug}").get_json()["snapshot"], original["snapshot"])

    def test_metadata_never_opens_duckdb_or_uses_global_profile_paths(self):
        slug = self.create()["slug"]
        self.write_input(slug)
        self.successful(slug)
        with patch("large_data_store.ThermalStore", side_effect=AssertionError("Metadata opened DuckDB")), \
                patch.object(profiles, "load_profile", side_effect=AssertionError("Global profile paths used")), \
                patch.object(profiles, "list_profiles", side_effect=AssertionError("Global profile paths used")):
            self.assertEqual(self.client.get("/").status_code, 200)
            self.assertEqual(self.client.get("/api/profiles").status_code, 200)
            self.assertEqual(self.client.get(f"/api/profiles/{slug}").status_code, 200)
            self.assertTrue(shared.publish_portal(self.root, self.publish).is_file())

    def test_static_html_safe_relative_links_and_only_successful_trees(self):
        malicious = '</script><img src=x onerror=alert(1)>&'
        slug = self.create(owner=malicious, notes=malicious)["slug"]
        self.write_input(slug)
        processed = self.successful(slug)
        state = self.root / "Saved_Sessions" / "profiles" / slug
        abandoned = state / "versions" / ("f" * 32)
        abandoned.mkdir()
        (abandoned / "private.txt").write_text("do not copy", encoding="utf-8")
        unrelated = self.publish / "user-file.txt"
        unrelated.write_text("keep", encoding="utf-8")
        other_publish = self.publish.parent / "offline-copy"
        index = shared.publish_portal(self.root, other_publish, "http://localhost:8051")
        text = index.read_text(encoding="utf-8")
        self.assertNotIn(malicious, text)
        self.assertIn("\\u003c/script\\u003e", text)
        self.assertIn("\\u0026", text)
        data = self.portal_data(text)
        self.assertEqual(data["service_url"], "http://localhost:8051")
        profile = data["profiles"][0]
        self.assertEqual(profile["owner"], malicious)
        self.assertEqual(profile["category_tests"], processed["category_tests"])
        self.assertEqual(profile["config_snapshot"], processed["config_snapshot"])
        for url in profile["reports"].values():
            self.assertTrue(url.startswith("reports/"))
            self.assertTrue((index.parent / url).is_file())
            self.assertTrue(urljoin(index.as_uri(), url).startswith(index.parent.as_uri() + "/reports/"))
        files = [path.relative_to(other_publish).as_posix() for path in other_publish.rglob("*") if path.is_file()]
        self.assertFalse(any(path.endswith((".csv", ".duckdb", ".wal", "private.txt")) for path in files))
        self.assertEqual(unrelated.read_text(), "keep")
        live = self.client.get("/").get_data(as_text=True)
        self.assertNotIn(malicious, live)
        self.assertEqual(self.portal_data(live)["profiles"][0]["reports"], processed["reports"])
        # A publisher failure cannot replace an existing valid index.
        previous = index.read_bytes()
        with patch.object(shared, "_copy_version", side_effect=OSError("copy failed")):
            with self.assertRaises(OSError):
                shared.publish_portal(self.root, other_publish)
        self.assertEqual(index.read_bytes(), previous)

    def test_legacy_profile_and_malformed_files_do_not_break_library(self):
        folder = self.root / "Profiles"
        folder.mkdir()
        (folder / "WW40.json").write_text(json.dumps({"work_week": "WW40", "lots": ["L1"],
                                                      "slug": "../../unsafe", "updated": "2026-01-01"}), encoding="utf-8")
        (folder / "invalid.json").write_text("{broken", encoding="utf-8")
        (folder / "bad.name.json").write_text("{}", encoding="utf-8")
        data = self.client.get("/api/profiles").get_json()
        self.assertEqual(len(data["profiles"]), 1)
        self.assertEqual(data["profiles"][0]["owner"], "Legacy / unknown")
        self.assertEqual(data["profiles"][0]["slug"], "WW40")
        path = folder / 'WW40.json'
        original = path.read_bytes()
        self.assertNotIn('data_domain', json.loads(original))
        self.write_input("WW40", [("DTS_S1", "5", "legacy", "L1", "Prog1", "P1")])
        processed = self.successful('WW40')
        self.assertEqual(processed['counts']['readings'], 1)
        self.assertEqual(processed['snapshot']['data_domain'], 'CLASS')
        self.assertEqual(processed['snapshot']['profile']['data_domain'], 'CLASS')
        # A domain-only body is valid; pull defaults to false.
        response = self.client.post('/api/profiles/WW40/process', json={'data_domain': 'SORT'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['snapshot']['data_domain'], 'SORT')
        self.assertFalse(response.get_json()['snapshot']['pull'])
        self.assertEqual(path.read_bytes(), original)
        self.fetcher.assert_not_called()

    def test_nonblocking_locks_across_apps_and_processes(self):
        slug = self.create()["slug"]
        second_client = shared.create_app(self.root, self.publish).test_client()
        lock_path = self.root / ".shared_dashboard.lock"
        with shared._file_lock(lock_path):
            token = lock_path.read_bytes()
            with shared._file_lock(shared._state_folder(self.root, slug) / '.processing.lock'):
                self.assertEqual(second_client.post(f"/api/profiles/{slug}/process", json={"pull": False}).status_code, 409)
            self.assertEqual(second_client.get("/api/profiles").status_code, 200)
            with self.assertRaises(shared.SharedDashboardError) as caught:
                shared.publish_portal(self.root, self.publish)
            self.assertEqual(caught.exception.status, 409)
            code = (
                "from pathlib import Path; import sys; import shared_dashboard as s\n"
                "try:\n"
                "    with s._file_lock(Path(sys.argv[1])): sys.exit(9)\n"
                "except s.SharedDashboardError as e:\n"
                "    sys.exit(0 if e.status == 409 else 8)\n"
            )
            completed = subprocess.run([sys.executable, "-B", "-c", code, str(lock_path)],
                                       cwd=Path(shared.__file__).parent, capture_output=True, text=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(lock_path.read_bytes(), token)
        self.assertFalse(lock_path.exists())
        self.write_input(slug)
        with shared._file_lock(self.publish / ".shared_dashboard.lock"):
            response = self.process(slug)
            self.assertEqual(response.status_code, 200)
            self.assertIn('publication is busy', response.get_json()['warning'])
            self.assertFalse(lock_path.exists())  # Earlier lock acquisition was unwound.

    def test_lock_does_not_remove_replacement_owned_by_someone_else(self):
        path = self.root / ".shared_dashboard.lock"
        with shared._file_lock(path):
            path.unlink()
            path.write_bytes(b"another-process-token")
        self.assertEqual(path.read_bytes(), b"another-process-token")

    def test_report_traversal_and_private_file_access_blocked(self):
        self.publish.mkdir()
        (self.publish / "private.txt").write_text("secret", encoding="utf-8")
        for path in ("/reports/../private.txt", "/reports/%2e%2e/private.txt",
                     "/reports/..%5Cprivate.txt", "/reports/C:/private.txt",
                     "/reports/.pending-hidden/dashboard.html", "/reports/missing.html"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertIn(response.status_code, (400, 404))
                self.assertNotIn("secret", response.get_data(as_text=True))

    def test_symlink_escape_is_rejected_if_platform_supports_symlinks(self):
        outside = self.root.parent / "outside"
        outside.mkdir()
        (outside / "secret.html").write_text("secret", encoding="utf-8")
        self.publish.mkdir()
        try:
            (self.publish / "reports").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks requires privileges on this Windows installation")
        response = self.client.get("/reports/secret.html")
        self.assertIn(response.status_code, (400, 404))
        self.assertNotIn("secret", response.get_data(as_text=True))

    def test_missing_template_does_not_damage_existing_index(self):
        index = shared.publish_portal(self.root, self.publish)
        before = index.read_bytes()
        (self.root / "assets" / "profile_portal.html").unlink()
        self.assertEqual(self.client.get("/").status_code, 503)
        with self.assertRaises(shared.SharedDashboardError):
            shared.publish_portal(self.root, self.publish)
        self.assertEqual(index.read_bytes(), before)

    def test_old_exporter_is_rejected_instead_of_unfiltered_fallback(self):
        self.exporter.stop()
        slug = self.create()["slug"]
        self.write_input(slug)

        def old_exporter(store, output_folder, product=None, source_note=None, plot_limit=5000):
            raise AssertionError("Unsafe old exporter must not run")

        with patch("export_dashboard_html.export_dashboard", old_exporter):
            response = self.process(slug)
        self.assertEqual(response.status_code, 503)
        self.assertIn("filters, sensor_config and limits", response.get_json()["error"])
        self.assertIsNone(self.client.get(f"/api/profiles/{slug}").get_json()["snapshot"])

    def test_real_exporter_integration(self):
        import category_reports
        import export_dashboard_html
        from test_category_reports import ReportParser

        self.exporter.stop()
        slug = self.create()["slug"]
        tests = {'thermal_diode': 'DTS_DIODE_S1', 'dts': 'DTS_S1', 'dtsmon': 'DTS_MON_S1'}
        rows = []
        for test in tests.values():
            rows.extend([(test, '91|121', 'unit', 'L1', 'Prog1', 'P1'),
                         (test, '999|999', 'excluded-lot', 'L2', 'Prog1', 'P1'),
                         (test, '999|999', 'excluded-program', 'L2', 'Prog2', 'P1'),
                         (test, '999|999', 'excluded-product', 'L1', 'Prog1', 'P2')])
        self.write_input(slug, rows)
        # Observe dispatch without replacing the real SQL/report implementations.
        # Signature-based compatibility checking requires autospec on the DTS writer.
        with patch.object(category_reports, 'export_diode', wraps=category_reports.export_diode) as diode, \
                patch.object(category_reports, 'export_dtsmon', wraps=category_reports.export_dtsmon) as dtsmon, \
                patch.object(category_reports, 'export_dts', wraps=category_reports.export_dts) as dts:
            processed = self.successful(slug)
        self.assertEqual(processed['counts']['readings'], 6)
        for category, writer in (('thermal_diode', diode), ('dts', dts), ('dtsmon', dtsmon)):
            writer.assert_called_once()
            self.assertEqual(writer.call_args.kwargs['filters'], {
                'Lot': ['L1'], 'Product_Name': 'P1', 'Test': [tests[category]]})
            self.assertEqual(writer.call_args.kwargs['limits'], self.limits)
            self.assertEqual(processed['category_counts'][category]['readings'], 2)
        parsed = {}
        for category, url in processed['reports'].items():
            with self.client.get(url) as response:
                self.assertEqual(response.status_code, 200)
                content = response.get_data(as_text=True)
            self.assertIn('plotly', content.lower())
            self.assertIn(shared.VALIDATION_NOTE, content)
            self.assertIn(f' | CLASS | {category} | ', content)
            self.assertNotIn('excluded-', content)
            parsed[category] = ReportParser(self.publish / url.lstrip('/'))
            self.assertFalse(parsed[category].external_scripts)
            self.assertEqual(len(parsed[category].figures), 1)
        histogram = parsed['thermal_diode'].figures[0]
        self.assertEqual(histogram['data'][0]['type'], 'bar')
        bars = [trace for trace in histogram['data'] if trace['type'] == 'bar']
        self.assertEqual(len(bars), 1)
        self.assertEqual(sum(bars[0]['y']), 2)
        self.assertEqual(len(histogram['data']), 1)
        self.assertEqual(histogram['layout']['meta']['mean'], 106)
        self.assertEqual(list(parsed['thermal_diode'].tables), ['Full-data product / test statistics', 'Recipe Revision'])
        scatter = parsed['dts'].figures[0]
        self.assertEqual(scatter['data'][0]['type'], 'scatter')
        self.assertEqual(set(scatter['data'][0]['x']), {'Unknown'})
        self.assertEqual(set(scatter['data'][0]['y']), {91, 121})
        self.assertEqual({row[0] for row in scatter['data'][0]['customdata']}, {'sensor_a', 'sensor_b'})
        self.assertEqual({row[1] for row in scatter['data'][0]['customdata']}, {'unit'})
        annotations = [item['text'] for item in scatter['layout']['annotations']]
        self.assertEqual(set(annotations), {'UCL', 'LCL'})
        self.assertEqual(scatter['layout']['meta']['total_readings'], 2)
        self.assertEqual({row['mean'] for row in parsed['dts'].tables['Full-data sensor statistics']}, {'91.0', '121.0'})
        maxima = parsed['dtsmon'].figures[0]
        self.assertEqual(maxima['data'][0]['type'], 'scatter')
        self.assertEqual(maxima['data'][0]['name'], 'DTSMax')
        self.assertEqual(maxima['data'][0]['y'], [121])
        self.assertEqual(maxima['data'][0]['customdata'], [['unit', 'sensor_b', 'L1', 'Unknown socket']])
        self.assertEqual(maxima['layout']['meta']['maximum_keys'], ['Unit_ID', 'Test', 'Socket'])
        summary = parsed['dtsmon'].tables['Per-lot summary'][0]
        self.assertEqual(summary['observations'], '1')
        self.assertEqual(summary['over_gt120'], '1')
        self.assertEqual(summary['under_lt90'], '0')
        self.assertEqual(summary['within_90_120'], '0')
        self.assertEqual(len(parsed['dtsmon'].links), 3)
        for link in parsed['dtsmon'].links:
            with self.client.get(urljoin(processed['reports']['dtsmon'], link)) as response:
                self.assertEqual(response.status_code, 200)
                content = response.get_data(as_text=True)
                self.assertNotIn('excluded-', content)
                if link.endswith('/all_winners.tsv'):
                    winners = list(csv.DictReader(content.splitlines(), delimiter='\t'))
                    self.assertEqual(len(winners), 1)
                    self.assertEqual(winners[0]['Test'], tests['dtsmon'])
                    self.assertEqual(float(winners[0]['DTSMax']), 121)


if __name__ == "__main__":
    unittest.main()