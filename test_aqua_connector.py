"""Offline tests of the current AQUA connector and its default Flask integration."""

import csv
import hashlib
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import aqua_connector as aqua
import profiles
import test_shared_dashboard as dashboard_tests


def settings():
    # Only passed to a mock: no installed CLI or network share is required.
    return {
        "executable": str(Path(sys.executable).parent / "AquaCmdLine.exe"),
        "report_path": "saved reports/thermal sensors",
        "arguments": ["-reportPath", "{report_path}", "-lots", "{lot}",
                      "-outputFileName", "{output}", "-server", "{server}",
                      "-program", "{test_program}", "-step", "{subflow_step}",
                      "-product", "{product}"],
        "timeout_seconds": 37,
    }


def write_settings(root, value):
    path = root / "config" / "aqua_cli.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def write_csv(path, lot="L1", test="DTS_S1", result="sensor_a:12.5|sensor_b:21",
              lot_column="Lot"):
    with Path(path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["TEST_NAME", "TEST_RESULT", "VISUAL_ID", lot_column,
                         "Program_Name", "Product_Name"])
        writer.writerow([test, result, "fresh-unit", lot, "Prog1", "P1"])


def output_path(command):
    return Path(command[command.index("-outputFileName") + 1])


def successful_cli(command, **kwargs):
    lot = command[command.index("-lots") + 1].split(",")[-1]
    write_csv(output_path(command), lot=lot)
    return subprocess.CompletedProcess(command, 0)


def write_parameters(root, domain, operations=None):
    # Synthetic offline exports, NOT verified production SORT operations/CLI flags.
    operations = operations or (['6248'] if domain == 'CLASS' else ['7777', '8888'])
    path = root / 'config' / aqua.PARAMETER_FILES[domain]
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ['@ Parameters : offline fixture', '@ Operations :', '']
    for operation in operations:
        lines += [f'\t@@ {operation}| : Data Domain = {domain}; Show Latest = Yes;\t',
                  '\t\t@@@ Selected Filters : ProgramName']
    lines += ['', '@ Parameters Group : Session Data',
              '\t@@ "0|Operation" : Operations = 9999;\t', '']
    path.write_bytes(('\ufeff' + '\r\n'.join(lines)).encode('utf-8'))
    return path


def domain_settings():
    value = settings()
    arguments = value.pop('arguments')
    value.pop('report_path')
    value['domains'] = {
        domain: {'report_path': f'saved reports/{domain} sensors',
                 'arguments': arguments + [f'offline-{domain}', '{parameter_file}',
                                           '{data_domain}', '{operations}']}
        for domain in ('CLASS', 'SORT')
    }
    return value


def write_metadata_csv(path, metadata, test='DTS_S1'):
    with Path(path).open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['TEST_NAME', 'TEST_RESULT', 'Lot', *metadata])
        writer.writerow([test, 'sensor:21', 'L1', *metadata.values()])


class AquaConnectorTests(unittest.TestCase):
    def test_query_report_replaces_saved_filters(self):
        raw = b'@ Attributes :\n\t@@ Lot :\n = OLD\n ;\n\t@@ ProgramName :\n = OLD_PROGRAM\n ;\n\t@@ LastNWwsStartDateTime :\n = 1\n ;\n@ Parameters : test\n\t@@@ Selected Filters : ProgramName\n'
        by_lot = aqua._query_report(raw, ['L1', 'L2'], '').decode()
        self.assertIn('= L1', by_lot)
        self.assertIn('= L2', by_lot)
        self.assertNotIn('ProgramName', by_lot)
        self.assertNotIn('OLD', by_lot)
        self.assertNotIn('LastNWws', by_lot)
        by_program = aqua._query_report(raw, [], 'NVLHX66C0H13K0*').decode()
        self.assertNotIn('@@ Lot', by_program)
        self.assertIn('= NVLHX66C0H13K0*', by_program)

    def test_program_only_mcp_and_returned_scope(self):
        parameter = write_parameters(self.root, 'CLASS')
        parameter.write_text('@ Report : fixture\n' + parameter.read_text(encoding='utf-8-sig') +
                             '\n@ Parameters Group : Parametric\n@@ "0|DTS_S1" : Operations = 6248;\n')
        value = settings()
        value.update(transport='mcp', domains={'CLASS': {'mode': 'report_config'}})
        write_settings(self.root, value)
        def execute(root, arguments, timeout):
            self.assertNotIn('lots', arguments)
            self.assertEqual(arguments['programNames'], 'Prog*')
            write_csv(arguments['outputfilename'])
            return {'message': 'Done'}
        with patch('mcp_connector.execute_aqua', side_effect=execute):
            files = self.fetch(data_domain='CLASS', lots=[], test_program='Prog*')
        self.assertEqual(len(files), 1)
        with self.assertRaisesRegex(aqua.AquaError, 'unexpected test program'):
            aqua._validate_download(files[0], None, {'DTS_S1'}, program='Different')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination = self.root / "fresh downloads"
        self.settings = settings()
        self.config = write_settings(self.root, self.settings)
        self.fields = {"source": "aqua", "lots": ["L1"], "test_program": "Prog1",
                       "subflow_step": "CLASSHOT", "product": "P1"}
        mock = patch.object(aqua.subprocess, "run", side_effect=successful_cli)
        self.run = mock.start()
        self.addCleanup(mock.stop)

    def fetch(self, **overrides):
        return aqua.fetch_profile(self.root, dict(self.fields, **overrides),
                                  self.destination, {"DTS_S1"})

    def test_parameter_export_stages_selected_sensors_and_uses_documented_cli(self):
        parameter = write_parameters(self.root, 'CLASS')
        parameter.write_text('@ Report : fixture\n' + parameter.read_text(encoding='utf-8-sig') +
                             '\n@ Parameters Group : Parametric\n'
                             '@@ "0|DTS_S1" : Operations = 6248;\n'
                             '@@ "1|OTHER_TEST" : Operations = 6248;\n'
                             '@ Parameters Group : String\n'
                             '@@ "0|IPC::*_DTS" : Operations = 6248;\n', encoding='utf-8')
        original = parameter.read_bytes()
        cache = self.root / 'server cache'
        value = settings()
        value['domains'] = {'CLASS': {'mode': 'parameter_export', 'cache_directory': str(cache)}}
        write_settings(self.root, value)
        files = self.fetch(data_domain='CLASS')
        self.assertEqual(len(files), 1)
        command = self.run.call_args.args[0]
        staged = Path(command[command.index('-reportPath') + 1])
        self.assertEqual(staged.parent, cache)
        self.assertIn('DTS_S1', staged.read_text())
        self.assertNotIn('OTHER_TEST', staged.read_text())
        self.assertNotIn('IPC::*_DTS', staged.read_text())
        self.assertIn('-aquaServer', command)
        self.assertEqual(command[command.index('-lots') + 1], 'L1')
        self.assertNotIn('-programNames', command)
        self.assertEqual(parameter.read_bytes(), original)
        retrieval = json.loads((self.destination / 'retrieval.json').read_text())
        self.assertEqual(retrieval['report']['tests'], ['DTS_S1'])
        self.assertEqual(retrieval['report']['sha256'], hashlib.sha256(staged.read_bytes()).hexdigest())
        self.assertEqual(retrieval['parameters']['operations'], ['6248'])

    def test_report_config_uses_local_sensor_export_without_cache(self):
        parameter = write_parameters(self.root, 'CLASS')
        parameter.write_text('@ Report : fixture\n' + parameter.read_text(encoding='utf-8-sig') +
                             '\n@ Parameters Group : Parametric\n'
                             '# Group Settings :  [ Data Type Position = None;  ]\n'
                             '@@ "0|DTS_S1" : Operations = 6248;\n'
                             '@@ "1|OTHER_TEST" : Operations = 6248;\n', encoding='utf-8')
        original = parameter.read_bytes()
        value = settings()
        value['domains'] = {'CLASS': {'mode': 'report_config'}}
        write_settings(self.root, value)
        files = self.fetch(data_domain='CLASS')
        command = self.run.call_args.args[0]
        report = Path(command[command.index('-ReportConfig') + 1])
        self.assertEqual(report.parent, self.destination.resolve())
        self.assertNotIn('-reportPath', command)
        self.assertIn('DTS_S1', report.read_text())
        self.assertIn('# Group Settings :  [ Data Type Position = None;  ]', report.read_text())
        self.assertNotIn('OTHER_TEST', report.read_text())
        self.assertEqual(command[command.index('-lots') + 1], 'L1')
        self.assertNotIn('-programNames', command)
        self.assertEqual(files, [self.destination.resolve() / 'aqua_L1.csv'])
        self.assertEqual(parameter.read_bytes(), original)
        retrieval = json.loads((self.destination / 'retrieval.json').read_text())
        self.assertEqual(retrieval['parameter_binding'], 'bound')
        self.assertEqual(retrieval['report']['mode'], 'report_config')
        self.assertEqual(retrieval['report']['tests'], ['DTS_S1'])
        self.assertEqual(retrieval['report']['sha256'], hashlib.sha256(report.read_bytes()).hexdigest())

    def test_mcp_transport_calls_tool_and_validates_csv(self):
        parameter = write_parameters(self.root, 'CLASS')
        equipment = {'Thermal Head Id': 'head-1', 'Unit Tester Id': 'tester-1',
                 'Unit Tester Site Id': 'A102'}
        parameter.write_text('@ Report : fixture\n' + parameter.read_text(encoding='utf-8-sig') +
                     ''.join(f'\n@@ "{index}|{name}" : Operations = 6248;' for index, name in enumerate(equipment, 1)) +
                             '\n@ Parameters Group : Parametric\n@@ "0|DTS_S1" : Operations = 6248;\n')
        value = settings()
        value.update(transport='mcp', domains={'CLASS': {'mode': 'report_config'}})
        write_settings(self.root, value)

        def execute(root, arguments, timeout):
            self.assertEqual(arguments['lots'], 'L1')
            self.assertNotIn('programNames', arguments)
            report = Path(arguments['ReportConfig']).read_text()
            self.assertIn('DTS_S1', report)
            for name in equipment:
                self.assertIn(name, report)
            write_metadata_csv(arguments['outputfilename'], {'OPERGROUP': '6248_CLASSHOT',
                               'Operation': '6248', 'VISUAL_ID': 'unit-1', **equipment})
            return {'message': 'Done'}

        with patch('mcp_connector.execute_aqua', side_effect=execute) as tool:
            files = self.fetch(data_domain='CLASS')
            self.assertEqual(len(files), 1)
        row = next(aqua._source_rows(files[0]))
        self.assertEqual(row['OPERGROUP'], '6248_CLASSHOT')
        for name, value in equipment.items():
            self.assertEqual(row[name], value)
        tool.assert_called_once()
        self.run.assert_not_called()
        receipt = json.loads((self.destination / 'retrieval.json').read_text())
        self.assertEqual(receipt['transport'], 'AQUA MCP')

    def test_class_report_retrieves_recipe_field_without_requiring_it_as_sensor(self):
        parameter = write_parameters(self.root, 'CLASS')
        parameter.write_text('@ Report : fixture\n' + parameter.read_text(encoding='utf-8-sig') +
                             '\n@ Parameters Group : Parametric\n@@ "0|DTS_S1" : Operations = 6248;\n'
                             '@ Parameters Group : String\n@@ "0|PCS_STIM_RECIPE_VERSION" : Operations = 6248;\n'
                             '@@ "1|OTHER_STRING" : Operations = 6248;\n', encoding='utf-8')
        value = settings()
        value['domains'] = {'CLASS': {'mode': 'report_config'}}
        write_settings(self.root, value)
        self.fetch(data_domain='CLASS')
        command = self.run.call_args.args[0]
        report = Path(command[command.index('-ReportConfig') + 1]).read_text()
        self.assertIn('PCS_STIM_RECIPE_VERSION', report)
        self.assertNotIn('OTHER_STRING', report)

    def test_sensor_report_narrows_wildcards_to_selected_dtsmon(self):
        raw = ('@ Report : fixture\n@ Parameters Group : String\n'
               '@@ "0|IPC::ARR_*::*_DTS" : Operations = 6248;\n'
               '@@ "1|UNRELATED*" : Operations = 6248;\n').encode('utf-8')
        test = 'IPC::ARR_CORE::TEMP_DTS'
        report, matched = aqua._sensor_report(raw, {test})
        self.assertEqual(matched, [test])
        self.assertIn(b'IPC::ARR_CORE::TEMP_DTS', report)
        self.assertNotIn(b'IPC::ARR_*::*_DTS', report)
        self.assertNotIn(b'UNRELATED', report)

    def test_trace_normalization_preserves_socket_names_and_raw_results(self):
        from mcp_connector import normalize_trace

        raw = self.root / 'trace.csv'
        raw.write_text('JobName,TestName,ResultType,VisualId,Value\n'
                       'job,DTS_S1,String,unit,SensorA:97|SensorB:102\n'
                       'job,OTHER,Parametric,unit,99\n', encoding='utf-8')
        for socket in ('CLASSHOT', 'CSM', 'PHMHOT', 'PHMCOLD', 'PHMROOM', ''):
            output = self.root / ('normalized-' + (socket or 'unknown') + '.csv')
            count = normalize_trace(raw, output, {'name': 'job', 'lot': 'L1',
                                    'programName': 'Prog1', 'partType': 'P1', 'processStep': socket}, {'DTS_S1'})
            self.assertEqual(count, 1)
            with output.open(newline='') as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row['Socket'], socket)
            self.assertEqual(row['TEST_RESULT'], 'SensorA:97|SensorB:102')

    def test_live_trace_chain_round_trips_jobs_and_reads_complete_csv(self):
        import mcp_connector as connector

        write_parameters(self.root, 'CLASS')
        raw = self.root / 'results.csv'
        raw.write_text('JobName,TestName,ResultType,VisualId,Value\n'
                       'job,DTS_S1,Parametric,unit,97\n', encoding='utf-8')
        job = {'name': 'job', 'source': 'Class', 'site': 'PG', 'dataSource': 'CLASSHDMT', 'id': None}
        session = Mock(call_tool=AsyncMock(return_value=Mock(
            content=[], isError=False, structuredContent={'endpoint': 'live', 'cliPath': 'trace-cli.exe'})))

        @asynccontextmanager
        async def connect(*args):
            yield session

        responses = [{'jobs': [job], 'totalCount': 1},
                     {'classJobs': [{'name': 'job', 'lot': 'L1', 'operation': '6248',
                                     'programName': 'Prog1', 'partType': 'P1', 'processStep': 'CLASSHOT'}]},
                     {'filePath': str(raw), 'totalCount': 1}]
        with patch.object(connector, 'connection', connect), patch.object(
                connector, 'trace_tool', new_callable=AsyncMock, side_effect=responses) as tool:
            files = connector.fetch_trace(self.root, dict(self.fields, data_domain='CLASS'),
                                          self.destination, {'DTS_S1'})
        self.assertEqual(len(files), 1)
        self.assertEqual([call.args[2] for call in tool.call_args_list],
                         ['find_jobs', 'get_detailed_jobs', 'get_test_results'])
        self.assertEqual(tool.call_args_list[1].args[3]['jobs'], [job])
        self.assertEqual(tool.call_args_list[2].args[3]['jobs'], [job])
        self.assertEqual(tool.call_args_list[0].args[3]['filter']['processStep'], 'CLASSHOT')
        self.assertNotIn('DTS_S1', tool.call_args_list[2].args[3]['testInstanceNames'])
        receipt = json.loads((self.destination / 'retrieval.json').read_text())
        self.assertEqual(receipt['source'], 'trace')
        self.assertEqual(receipt['jobs'], [job])

    def test_trace_equipment_never_assigns_ambiguous_job_testers(self):
        from mcp_connector import normalize_trace

        raw = self.root / 'trace.csv'
        raw.write_text('JobName,TestName,ResultType,VisualId,Value,ThermalHeadId,Socket\n'
                       'job,DTS_S1,Parametric,unit,97,head-7,phm cold\n', encoding='utf-8')
        for index, testers in enumerate((['tester-1'], ['tester-1', 'tester-2'])):
            output = self.root / f'equipment-{index}.csv'
            normalize_trace(raw, output, {'name': 'job', 'lot': 'L1', 'processStep': 'CLASSHOT',
                                           'testers': testers}, {'DTS_S1'})
            with output.open(newline='') as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row['Unit Tester Id'], 'tester-1' if index == 0 else '')
            self.assertEqual(row['Thermal Head Id'], 'head-7')
            self.assertEqual(row['Socket'], 'PHMCOLD')

    def test_sensor_report_preserves_format_when_last_group_is_removed(self):
        report_format = ('@ Report Format\r\n@ Data Tracking Level : Units\r\n'
                         '@ Format : Raw Data\r\n@ Report Output Format : Csv\r\n'
                         '@ Pivot SSID : NO\r\n@ Chunk Files : NO\r\n'
                         '@ Stack Session Data : NO\r\n')
        raw = ('@ Report : fixture\r\n@ Parameters Group : Parametric\r\n'
               '@@ "0|DTS_S1" : Operations = 7777;\r\n'
               '@ Parameters Group : String\r\n@@ "0|OTHER_TEST" :\r\n' +
               report_format + '@ Post Processing Definition - Please IGNORE\r\n').encode('utf-8')
        report, matched = aqua._sensor_report(raw, {'DTS_S1'})
        self.assertEqual(matched, ['DTS_S1'])
        self.assertIn(report_format.encode('utf-8'), report)
        self.assertIn(b'@ Post Processing Definition - Please IGNORE', report)
        self.assertNotIn(b'@ Parameters Group : String', report)
        self.assertNotIn(b'OTHER_TEST', report)

    def test_sdx_report_config_uses_lots_and_retains_selected_sensors(self):
        parameter = write_parameters(self.root, 'SORT')
        parameter.write_text('@ Report : fixture\n@ Domain : PreAssembly\n' +
                             parameter.read_text(encoding='utf-8-sig').replace('Data Domain = SORT', 'Data Domain = SDX') +
                             '\n@ Parameters Group : Parametric\n'
                             '@@ "0|DTS_S1" : Operations = 7777;\n'
                             '@@ "1|OTHER_TEST" : Operations = 7777;\n', encoding='utf-8')
        original = parameter.read_bytes()
        value = settings()
        value['domains'] = {'SORT': {'mode': 'report_config'}}
        write_settings(self.root, value)

        def download(command, **kwargs):
            self.assertNotIn('-ults', command)
            self.assertEqual(command[command.index('-lots') + 1], 'L1')
            self.assertNotIn('-programNames', command)
            report = Path(command[command.index('-ReportConfig') + 1])
            self.assertIn('DTS_S1', report.read_text())
            self.assertEqual(report.read_bytes(), original)
            write_csv(output_path(command), lot='L1')
            return subprocess.CompletedProcess(command, 0)

        self.run.side_effect = download
        self.assertEqual(self.fetch(data_domain='SORT'), [self.destination.resolve() / 'aqua_L1.csv'])
        self.assertEqual(parameter.read_bytes(), original)
        retrieval = json.loads((self.destination / 'retrieval.json').read_text())
        self.assertEqual(retrieval['report']['selection'], 'complete_export')
        self.assertEqual(retrieval['report']['configured_tests'], ['DTS_S1'])
        self.assertNotIn('tests', retrieval['report'])

    def test_repository_sort_diode_config_includes_returned_delta_tests(self):
        root = Path(__file__).resolve().parent
        configured = json.loads((root / 'config' / 'Thermal_Diode.json').read_text(encoding='utf-8'))
        tests = {entry['TEST_NAME'] for entry in configured}
        definition = aqua.parameter_definition(root, 'SORT')
        report, matched = aqua._sensor_report(definition['path'].read_bytes(), tests)
        for cpu_index in (0, 1):
            name = (f'TH_TD_CPU_TDAU{cpu_index}_PTH_DIODE_CJ816P::'
                    'DIODE_X_TSINGLEMEASUREMENT_K_START_X_X_X_X_SOT_SDS_ERTD_DELTA')
            self.assertIn(name, tests)
            self.assertIn(name, matched)
        self.assertIn(b'@ Format : Raw Data', report)
        self.assertIn(b'@ Report Output Format : Csv', report)

    def test_server_report_uses_documented_cli_without_cache_or_local_filter_binding(self):
        parameter = write_parameters(self.root, 'CLASS')
        original = parameter.read_bytes()
        value = settings()
        value['domains'] = {'CLASS': {'mode': 'server_report', 'report_path': 'reports/diode CLASS'}}
        write_settings(self.root, value)
        with patch.object(aqua, '_sensor_report', side_effect=AssertionError('Must not stage a report')):
            files = self.fetch(data_domain='CLASS')
        command = self.run.call_args.args[0]
        self.assertEqual(command, [value['executable'], '-aquaServer', 'GAR', '-reportPath',
                                   'reports/diode CLASS', '-lots', 'L1', '-outputFileName', str(files[0])])
        self.assertEqual(parameter.read_bytes(), original)
        retrieval = json.loads((self.destination / 'retrieval.json').read_text())
        self.assertEqual(retrieval['parameter_binding'], 'unverified_server_report')
        self.assertEqual(retrieval['report']['filters'], 'server_owned')
        self.assertEqual(retrieval['report']['subflow_step'], 'unverified')
        self.assertEqual(retrieval['files'][0]['validation']['data_domain'], 'unverified')

    def test_server_report_environment_default_and_explicit_path_precedence(self):
        write_parameters(self.root, 'CLASS')
        value = settings()
        value['domains'] = {'CLASS': {'mode': 'server_report'}}
        write_settings(self.root, value)
        with patch.dict(aqua.os.environ, {'AQUA_REPORT_PATH': 'reports/CLASS diode'}):
            self.assertEqual(aqua.load_settings(self.root, 'CLASS')['report_path'], 'reports/CLASS diode')
            value['domains']['CLASS']['report_path'] = 'reports/explicit diode'
            write_settings(self.root, value)
            self.assertEqual(aqua.load_settings(self.root, 'CLASS')['report_path'], 'reports/explicit diode')

    def test_server_report_rejects_missing_path_and_parameter_file_as_report(self):
        parameter = write_parameters(self.root, 'CLASS')
        for report in ('', str(parameter)):
            with self.subTest(report=report), patch.dict(aqua.os.environ, {'AQUA_REPORT_PATH': ''}):
                value = settings()
                value['domains'] = {'CLASS': {'mode': 'server_report', 'report_path': report}}
                write_settings(self.root, value)
                with self.assertRaises(aqua.AquaError):
                    self.fetch(data_domain='CLASS')
        self.run.assert_not_called()

    def test_server_report_rejects_wrong_domain_in_sensor_download(self):
        write_parameters(self.root, 'CLASS')
        value = settings()
        value['domains'] = {'CLASS': {'mode': 'server_report', 'report_path': 'reports/diode'}}
        write_settings(self.root, value)
        def wrong_domain(command, **kwargs):
            write_metadata_csv(output_path(command), {'Data Domain': 'SORT', 'Operation': '6248'})
            return subprocess.CompletedProcess(command, 0)
        self.run.side_effect = wrong_domain
        with self.assertRaisesRegex(aqua.AquaError, 'unexpected data domain'):
            self.fetch(data_domain='CLASS')
        self.assertFalse((self.destination / 'retrieval.json').exists())

    def test_parameter_export_rejects_missing_report_and_unmatched_tests(self):
        for raw in (b'@ Parameters Group : Parametric\n@@ "0|DTS_S1" : Operations = 6248;',
                    b'@ Report : fixture\n@ Parameters Group : Parametric\n@@ "0|OTHER" : Operations = 6248;'):
            with self.subTest(raw=raw):
                with self.assertRaises(aqua.AquaError):
                    aqua._sensor_report(raw, {'DTS_S1'})
        self.run.assert_not_called()

    def test_load_settings_valid_bom_defaults_and_boundaries(self):
        for timeout in (None, 1, 14400):
            for server in (None, "GAR", "AMR", "GER"):
                with self.subTest(timeout=timeout, server=server):
                    value = settings()
                    if timeout is None:
                        value.pop("timeout_seconds")
                    else:
                        value["timeout_seconds"] = timeout
                    if server is not None:
                        value["server"] = server
                    self.config.write_text(json.dumps(value), encoding="utf-8-sig")
                    self.assertEqual(aqua.load_settings(self.root), value)
        self.run.assert_not_called()

    def test_missing_config(self):
        self.config.unlink()
        with self.assertRaisesRegex(aqua.AquaError, "not configured"):
            aqua.load_settings(self.root)
        with self.assertRaisesRegex(aqua.AquaError, "not configured"):
            self.fetch()
        self.run.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_malformed_json_and_non_object_settings(self):
        for text in ("", "{broken", '{"executable":'):
            with self.subTest(text=text):
                self.config.write_text(text, encoding="utf-8")
                # Current implementation deliberately exposes JSONDecodeError.
                with self.assertRaises(json.JSONDecodeError):
                    aqua.load_settings(self.root)
        for value in ([], [1], None, True, 123, "command"):
            with self.subTest(value=value):
                write_settings(self.root, value)
                with self.assertRaisesRegex(aqua.AquaError, "must be an object"):
                    aqua.load_settings(self.root)
        self.run.assert_not_called()

    def test_required_keys_and_argument_types(self):
        for key in ("executable", "report_path", "arguments"):
            for value in (None, "", [], False):
                with self.subTest(key=key, value=value):
                    write_settings(self.root, dict(settings(), **{key: value}))
                    with self.assertRaisesRegex(aqua.AquaError, "requires " + key):
                        aqua.load_settings(self.root)
            value = settings()
            del value[key]
            write_settings(self.root, value)
            with self.assertRaisesRegex(aqua.AquaError, "requires " + key):
                aqua.load_settings(self.root)
        for value in ("-lots {lot}", ["{lot}", 1], {"lot": "L1"}):
            with self.subTest(arguments=value):
                write_settings(self.root, dict(settings(), arguments=value))
                with self.assertRaisesRegex(aqua.AquaError, "list of separate"):
                    aqua.load_settings(self.root)
        for key in ("executable", "report_path"):
            write_settings(self.root, dict(settings(), **{key: 123}))
            with self.assertRaisesRegex(aqua.AquaError, "must be strings"):
                aqua.load_settings(self.root)
        self.run.assert_not_called()

    def test_strict_argument_templates(self):
        for template in ("{unknown}", "{lot.name}", "{lot[0]}", "{}", "{0}",
                         "{lot!r}", "{lot!s}", "{lot:>10}", "{lot:{product}}",
                         "{", "}", "prefix{lot"):
            with self.subTest(template=template):
                value = settings()
                value["arguments"].append(template)
                write_settings(self.root, value)
                with self.assertRaisesRegex(aqua.AquaError, "Invalid AQUA argument template"):
                    aqua.load_settings(self.root)
        for required in ("lot", "output", "report_path"):
            with self.subTest(missing=required):
                value = settings()
                value["arguments"].remove("{" + required + "}")
                write_settings(self.root, value)
                with self.assertRaisesRegex(aqua.AquaError, "arguments must include"):
                    aqua.load_settings(self.root)
        value = settings()
        value["arguments"].append("{{literal}}")
        write_settings(self.root, value)
        self.assertEqual(aqua.load_settings(self.root), value)
        self.run.assert_not_called()

    def test_invalid_timeout_and_server(self):
        for timeout in (True, False, 0, -1, 14401, 1.5, "37", None, [], {}):
            with self.subTest(timeout=timeout):
                write_settings(self.root, dict(settings(), timeout_seconds=timeout))
                with self.assertRaisesRegex(aqua.AquaError, "timeout_seconds"):
                    aqua.load_settings(self.root)
        for server in ("gar", "UNKNOWN", "", None, 1, [], {}):
            with self.subTest(server=server):
                write_settings(self.root, dict(settings(), server=server))
                with self.assertRaisesRegex(aqua.AquaError, "server must be"):
                    aqua.load_settings(self.root)
        self.run.assert_not_called()

    def test_invalid_lots_and_program_only_are_rejected(self):
        for lots in ([], None, 'L1', {'L1': 1}, [""], [123], [None], ["L-1"], ["L_1"], [" L1"],
                     ["L1 "], ["../L1"], ["L1;echo"], ["L1\n"], ["Lé1"], ["L1", "bad lot"]):
            with self.subTest(lots=lots):
                with self.assertRaisesRegex(aqua.AquaError, "alphanumeric|Program-only"):
                    self.fetch(lots=lots)
        fields = dict(self.fields)
        del fields["lots"]
        with self.assertRaisesRegex(aqua.AquaError, "Program-only"):
            aqua.fetch_profile(self.root, fields, self.destination, {"DTS_S1"})
        self.run.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_trace_rejected_before_loading_config(self):
        self.config.unlink()
        with self.assertRaisesRegex(aqua.AquaError, "TRACE retrieval is not configured"):
            self.fetch(source="trace")
        self.run.assert_not_called()

    def test_executable_requires_absolute_exe_path(self):
        for executable in ("AquaCmdLine.exe", str(Path(sys.executable).with_suffix(".cmd"))):
            with self.subTest(executable=executable):
                write_settings(self.root, dict(settings(), executable=executable))
                with self.assertRaisesRegex(aqua.AquaError, "absolute path"):
                    self.fetch()
        self.run.assert_not_called()

    def test_successful_named_sensors_and_exact_safe_command(self):
        self.fields["test_program"] = "Prog1 & not-a-shell-command"
        files = self.fetch()
        expected = self.destination.resolve() / "aqua_L1.csv"
        self.assertEqual(files, [expected])
        self.assertIn("sensor_a:12.5|sensor_b:21", expected.read_text())
        self.run.assert_called_once_with(
            [self.settings["executable"], "-reportPath", self.settings["report_path"],
             "-lots", "L1", "-outputFileName", str(expected), "-server", "GAR",
             "-program", "", "-step", "CLASSHOT", "-product", "P1"],
            cwd=self.destination.resolve(), shell=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=37, check=False)
        self.assertTrue(Path(self.run.call_args.args[0][0]).is_absolute())

    def test_multiple_lots_are_batched_into_few_requests(self):
        value = settings()
        value.pop("timeout_seconds")
        value["server"] = "AMR"
        write_settings(self.root, value)
        lots = ["L1", "P642374MV", "abc123"]
        files = self.fetch(lots=lots)
        self.assertEqual(files, [self.destination.resolve() / "aqua_batch1.csv"])
        self.run.assert_called_once()
        command = self.run.call_args.args[0]
        self.assertEqual(command[command.index("-lots") + 1], "L1,P642374MV,abc123")
        self.assertEqual(command[command.index("-server") + 1], "AMR")
        self.assertEqual(self.run.call_args.kwargs["timeout"], 3600)
        self.run.reset_mock()
        write_settings(self.root, dict(value, lot_batch_size=2))
        self.destination = self.root / "batched"
        files = self.fetch(lots=lots)
        self.assertEqual([path.name for path in files], ["aqua_batch1.csv", "aqua_abc123.csv"])
        self.assertEqual([call.args[0][call.args[0].index("-lots") + 1] for call in self.run.call_args_list],
                         ["L1,P642374MV", "abc123"])
        with self.assertRaisesRegex(aqua.AquaError, "unexpected lot"):
            aqua._validate_download(files[1], {"L1"}, {"DTS_S1"})

    def test_existing_download_is_never_reused(self):
        self.destination.mkdir()
        output = self.destination / "aqua_L1.csv"
        write_csv(output)
        before = output.read_bytes()
        with self.assertRaisesRegex(aqua.AquaError, "destination must be fresh"):
            self.fetch()
        self.assertEqual(output.read_bytes(), before)
        self.run.assert_not_called()

    def test_timeout_launch_error_and_exit_failure(self):
        for error, message in ((subprocess.TimeoutExpired("mock-cli", 37), "timed out"),
                               (OSError("mock launch failed"), "Cannot launch")):
            with self.subTest(error=type(error).__name__):
                self.run.side_effect = error
                with self.assertRaisesRegex(aqua.AquaError, message) as raised:
                    self.fetch()
                self.assertIs(raised.exception.__cause__, error)
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess([], 7)
        with self.assertRaisesRegex(aqua.AquaError, "exit code 7.*no previous data"):
            self.fetch()

    def test_failed_exit_rejects_even_valid_output(self):
        def failed(command, **kwargs):
            successful_cli(command, **kwargs)
            return subprocess.CompletedProcess(command, 9)
        self.run.side_effect = failed
        with self.assertRaisesRegex(aqua.AquaError, "exit code 9"):
            self.fetch()

    def test_missing_empty_and_directory_outputs(self):
        for kind in ("missing", "empty", "directory"):
            with self.subTest(kind=kind):
                self.destination = self.root / kind
                def cli(command, **kwargs):
                    if kind == "empty":
                        output_path(command).touch()
                    elif kind == "directory":
                        output_path(command).mkdir()
                    return subprocess.CompletedProcess(command, 0)
                self.run.side_effect = cli
                with self.assertRaisesRegex(aqua.AquaError, "did not create the requested CSV"):
                    self.fetch()

    def test_invalid_sensor_reports(self):
        cases = {
            "wrong-lot": dict(lot="L2"),
            "missing-lot": dict(lot=""),
            "missing-lot-column": dict(lot_column="not_a_lot"),
            "unknown-test": dict(test="UNCONFIGURED"),
            "nonfinite": dict(result="sensor_a:nan|sensor_b:inf|sensor_c:-inf|sensor_d:1e999"),
            "nonnumeric": dict(result="sensor_a:bad|sensor_b:"),
            "empty-result": dict(result=""),
            "header-only": None,
            "metadata-only": None,
            "mixed-lots": None,
        }
        for kind, changes in cases.items():
            with self.subTest(kind=kind):
                self.destination = self.root / kind
                def cli(command, **kwargs):
                    path = output_path(command)
                    if kind == "header-only":
                        path.write_text("TEST_NAME,TEST_RESULT,Lot\n", encoding="utf-8")
                    elif kind == "metadata-only":
                        path.write_text("LOTS,Socket,LOTS_End_Date_Time\nL1,0,2026-11-13\n", encoding="utf-8")
                    elif kind == "mixed-lots":
                        write_csv(path)
                        with path.open("a", encoding="utf-8") as stream:
                            stream.write("UNCONFIGURED,99,other-unit,L2,Prog1,P1\n")
                    else:
                        write_csv(path, **changes)
                    return subprocess.CompletedProcess(command, 0)
                self.run.side_effect = cli
                message = "missing or unexpected lot" if "lot" in kind else "no finite sensor readings"
                if kind == "metadata-only":
                    message = "TEST_NAME and TEST_RESULT are required"
                if message == 'TEST_NAME and TEST_RESULT are required':
                    message = 'TEST_NAME and TEST_RESULT or TEST_RESULT_NUMERIC are required'
                with self.assertRaisesRegex(aqua.AquaError, message):
                    self.fetch()

    def test_lots_alias_and_at_least_one_finite_reading(self):
        def cli(command, **kwargs):
            write_csv(output_path(command), lot_column="LOTS", result="sensor_a:nan|sensor_b:0|bad")
            return subprocess.CompletedProcess(command, 0)
        self.run.side_effect = cli
        self.assertEqual(self.fetch(), [self.destination.resolve() / "aqua_L1.csv"])


class AquaDomainTests(unittest.TestCase):
    fetch = AquaConnectorTests.fetch

    def setUp(self):
        AquaConnectorTests.setUp(self)
        self.parameters = {domain: write_parameters(self.root, domain) for domain in ('CLASS', 'SORT')}
        self.settings = domain_settings()
        write_settings(self.root, self.settings)
        self.fields['data_domain'] = 'CLASS'

    def provenance(self):
        return json.loads((self.destination / 'retrieval.json').read_text(encoding='utf-8'))

    def test_actual_exports_use_requested_filenames_and_preserve_source_domains(self):
        root = Path(__file__).resolve().parent
        definition = aqua.parameter_definition(root, 'CLASS')
        self.assertEqual(definition['operations'], ['6248'])
        self.assertEqual(definition['data_domain'], 'CLASS')
        self.assertEqual(definition['sha256'], hashlib.sha256(definition['path'].read_bytes()).hexdigest())
        self.assertEqual(definition['path'].name, 'NVL_Aqua_Class_Automation_Parameters.txt')
        sort = aqua.parameter_definition(root, 'SORT')
        self.assertEqual(sort['path'].name, 'NVL_Aqua_Sort_Automation_Parameters.txt')
        self.assertEqual(sort['data_domain'], 'SORT')
        self.assertEqual(sort['source_data_domain'], 'SDX')
        self.assertEqual(sort['operations'], ['132322', '119325', '132330', '132323'])
        self.run.assert_not_called()

    def test_sort_sdx_download_validates_declared_source_and_preserves_bytes(self):
        path = self.parameters['SORT']
        original = '@ Domain : PreAssembly\n' + path.read_text(encoding='utf-8-sig').replace(
            'Data Domain = SORT', 'Data Domain = SDX')
        path.write_text(original, encoding='utf-8')

        def download(command, **kwargs):
            self.assertIn('SDX', command)
            write_metadata_csv(output_path(command), {'Operation': '7777', 'Data Domain': 'SDX'})
            return subprocess.CompletedProcess(command, 0)

        self.run.side_effect = download
        self.fetch(data_domain='SORT')
        provenance = self.provenance()
        self.assertEqual(provenance['data_domain'], 'SORT')
        self.assertEqual(provenance['source_data_domain'], 'SDX')
        self.assertEqual(provenance['files'][0]['validation']['data_domain'], 'validated')
        self.assertEqual((self.destination / path.name).read_bytes(), path.read_bytes())

    def test_sort_sdx_requires_preassembly_and_rejects_mixed_domains(self):
        path = self.parameters['SORT']
        text = path.read_text(encoding='utf-8-sig').replace('Data Domain = SORT', 'Data Domain = SDX')
        path.write_text(text, encoding='utf-8')
        with self.assertRaisesRegex(aqua.AquaError, 'PreAssembly'):
            aqua.parameter_definition(self.root, 'SORT')
        path.write_text('@ Domain : PreAssembly\n' + text.replace('Data Domain = SDX', 'Data Domain = SORT', 1),
                        encoding='utf-8')
        with self.assertRaisesRegex(aqua.AquaError, 'mix source data domains'):
            aqua.parameter_definition(self.root, 'SORT')

    def test_sort_sdx_csv_rejects_other_domains_and_operations(self):
        path = self.root / 'sensor.csv'
        for metadata in ({'Operation': '7777', 'Data Domain': 'CLASS'},
                         {'Operation': '7777', 'Data Domain': 'SORT'},
                         {'Operation': '6248', 'Data Domain': 'SDX'}):
            write_metadata_csv(path, metadata)
            with self.assertRaises(aqua.AquaError):
                aqua._validate_download(path, 'L1', {'DTS_S1'}, 'SDX', ['7777', '8888'])

    def test_definition_uses_only_operations_block_and_exact_bytes(self):
        for domain, operations in (('CLASS', ['6248']), ('SORT', ['7777', '8888'])):
            with self.subTest(domain=domain):
                definition = aqua.parameter_definition(self.root, domain)
                self.assertEqual(definition, {'path': self.parameters[domain].resolve(),
                    'sha256': hashlib.sha256(self.parameters[domain].read_bytes()).hexdigest(),
                    'operations': operations, 'data_domain': domain})
        self.assertEqual(aqua.parameter_definition(self.root)['data_domain'], 'CLASS')

    def test_invalid_domains_and_explicit_null_rejected(self):
        for domain in ('class', 'sort', '', 'TRACE', '../SORT', 1, [], {}, True):
            with self.subTest(domain=domain):
                for call in (lambda: aqua.parameter_definition(self.root, domain),
                             lambda: aqua.load_settings(self.root, domain),
                             lambda: self.fetch(data_domain=domain)):
                    with self.assertRaisesRegex(aqua.AquaError, 'data_domain must be CLASS or SORT'):
                        call()
        with self.assertRaisesRegex(aqua.AquaError, 'data_domain must be CLASS or SORT'):
            self.fetch(data_domain=None)
        self.run.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_missing_and_malformed_parameter_files_fail_before_cli(self):
        path = self.parameters['CLASS']
        path.unlink()
        with self.assertRaisesRegex(aqua.AquaError, 'parameter file is missing'):
            self.fetch()
        malformed = ['', '@ Parameters Group : Bin\n@@ "0|a" : Operations = 6248;',
                     '@ Operations :\n', '@ Operations :\n@ Operations :\n',
                     '@ Operations :\n@@ 6248| : Data Domain = CLASS;\n@ Operations : malformed',
                     '@ Operations :\n@@ bad| : Data Domain = CLASS;',
                     '@ Operations :\n@@ 6248| : Data Domain = CLASS',
                     '@ Operations :\n@@ 6248| : Show Latest = Yes;',
                     '@ Operations :\n@@ 6248| : Data Domain = SORT;',
                     '@ Operations :\n@@ 6248| : Data Domain = CLASS; Data Domain = SORT;',
                     '@ Operations :\n@@ 6248| : Data Domain = CLASS;\n@@ 6248| : Data Domain = CLASS;',
                     '@ Operations :\n@@ 6248| : Data Domain = CLASS;\n@@ 7777| : Data Domain = SORT;',
                     '@ Operations :\n@@ 6248| : Data Domain = CLASS;\nunparsed operation']
        for text in malformed:
            with self.subTest(text=text):
                path.write_text(text, encoding='utf-8')
                with self.assertRaises(aqua.AquaError):
                    self.fetch()
        path.write_bytes(b'\xff\xfeinvalid')
        with self.assertRaisesRegex(aqua.AquaError, 'Cannot read'):
            self.fetch()
        self.run.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_sort_rejects_class_metadata_even_with_sort_configuration(self):
        self.parameters['SORT'].write_bytes(self.parameters['CLASS'].read_bytes())
        with self.assertRaisesRegex(aqua.AquaError, 'Data Domain mismatch'):
            aqua.load_settings(self.root, 'SORT')
        with self.assertRaisesRegex(aqua.AquaError, 'Data Domain mismatch'):
            self.fetch(data_domain='SORT')
        self.run.assert_not_called()

    def test_domain_settings_override_report_and_arguments_only(self):
        self.settings.update(report_path='wrong common report', arguments=['wrong-common-command'])
        write_settings(self.root, self.settings)
        for domain in ('CLASS', 'SORT'):
            selected = aqua.load_settings(self.root, domain)
            self.assertEqual(selected['report_path'], self.settings['domains'][domain]['report_path'])
            self.assertEqual(selected['arguments'], self.settings['domains'][domain]['arguments'])
            self.assertEqual(selected['executable'], self.settings['executable'])
            self.assertEqual(selected['timeout_seconds'], 37)
            self.assertEqual(selected['data_domain'], domain)
        self.assertEqual(aqua.load_settings(self.root)['data_domain'], 'CLASS')
        for value in (None, [], {'WRONG': {}}, {'SORT': {}}, {'CLASS': []},
                      {'CLASS': {'executable': 'untrusted.exe'}}, {'CLASS': {}},
                      {'CLASS': {'report_path': 'saved report', 'arguments': []}}):
            with self.subTest(domains=value):
                write_settings(self.root, dict(self.settings, domains=value))
                with self.assertRaises(aqua.AquaError):
                    aqua.load_settings(self.root)
        self.run.assert_not_called()

    def test_repository_class_and_sort_exports_are_configured(self):
        root = Path(__file__).resolve().parent
        configured = aqua.load_settings(root, 'CLASS')
        self.assertEqual(configured['mode'], 'report_config')
        self.assertIn('-ReportConfig', configured['arguments'])
        self.assertEqual(configured['_parameter_definition']['operations'], ['6248'])
        sort = aqua.load_settings(root, 'SORT')
        self.assertEqual(sort['mode'], 'report_config')
        self.assertIn('-ReportConfig', sort['arguments'])
        self.assertEqual(sort['_parameter_definition']['source_data_domain'], 'SDX')
        self.run.assert_not_called()

    def test_parameter_binding_required_in_explicit_and_domain_commands(self):
        for explicit, configured in ((True, False), (True, True), (False, True)):
            with self.subTest(explicit=explicit, configured=configured):
                value = domain_settings() if configured else settings()
                if configured:
                    value['domains']['CLASS']['arguments'].remove('{parameter_file}')
                write_settings(self.root, value)
                fields = dict(self.fields)
                if not explicit:
                    fields.pop('data_domain')
                with self.assertRaisesRegex(aqua.AquaError, r'must include \{parameter_file\}'):
                    aqua.fetch_profile(self.root, fields, self.destination, {'DTS_S1'})
        self.run.assert_not_called()

    def test_required_and_new_placeholder_validation(self):
        for missing in ('lot', 'output', 'report_path', 'parameter_file'):
            with self.subTest(missing=missing):
                value = domain_settings()
                value['domains']['CLASS']['arguments'].remove('{' + missing + '}')
                write_settings(self.root, value)
                with self.assertRaisesRegex(aqua.AquaError, 'must include'):
                    self.fetch()
        for template in ('{parameter_file.name}', '{data_domain!r}', '{operations[0]}', '{operations:>4}'):
            value = domain_settings()
            value['domains']['CLASS']['arguments'].append(template)
            write_settings(self.root, value)
            with self.assertRaisesRegex(aqua.AquaError, 'Invalid AQUA argument template'):
                self.fetch()
        self.run.assert_not_called()

    def test_parameter_export_cannot_be_the_saved_report(self):
        for report in (str(self.parameters['CLASS']), str(self.parameters['SORT']), '{parameter_file}'):
            value = domain_settings()
            value['domains']['CLASS']['report_path'] = report
            write_settings(self.root, value)
            with self.assertRaisesRegex(aqua.AquaError, 'must be a saved report'):
                self.fetch()
        for argument in ('{parameter_file}', '-reportPath={parameter_file}', '-reportPath:{parameter_file}'):
            value = domain_settings()
            value['domains']['CLASS']['arguments'] += ['-reportPath', argument]
            write_settings(self.root, value)
            with self.assertRaisesRegex(aqua.AquaError, 'cannot be passed as reportPath'):
                self.fetch()
        self.run.assert_not_called()

    def test_exact_domain_substitutions_frozen_bytes_and_safe_provenance(self):
        for domain in ('CLASS', 'SORT'):
            with self.subTest(domain=domain):
                self.destination = self.root / f'fresh {domain}'
                self.run.reset_mock()
                self.fields.update(data_domain=domain, operations='9999', parameter_file='untrusted',
                                   test_program='program secret not for provenance')
                raw = self.parameters[domain].read_bytes()
                files = self.fetch()
                frozen = self.destination.resolve() / self.parameters[domain].name
                operations = ['6248'] if domain == 'CLASS' else ['7777', '8888']
                selected = self.settings['domains'][domain]
                values = dict(self.fields, lot='L1', output=str(files[0]), report_path=selected['report_path'],
                              server='GAR', parameter_file=str(frozen), operations=','.join(operations), test_program='')
                expected_command = [self.settings['executable'],
                                    *[arg.format_map(values) for arg in selected['arguments']]]
                self.assertEqual(self.run.call_args.args[0], expected_command)
                self.assertFalse(self.run.call_args.kwargs['shell'])
                self.assertEqual(frozen.read_bytes(), raw)
                self.assertFalse(frozen.stat().st_mode & stat.S_IWRITE)
                self.assertEqual(files, [self.destination.resolve() / 'aqua_L1.csv'])
                provenance = self.provenance()
                self.assertEqual(provenance['data_domain'], domain)
                self.assertEqual(provenance['parameter_binding'], 'bound')
                self.assertEqual(provenance['parameters'], {'filename': frozen.name,
                    'sha256': hashlib.sha256(raw).hexdigest(), 'operations': operations, 'data_domain': domain})
                self.assertEqual(provenance['files'], [{'filename': 'aqua_L1.csv',
                    'validation': {'operation': 'unverified', 'data_domain': 'unverified'}}])
                text = json.dumps(provenance)
                for secret in (self.settings['executable'], selected['report_path'], self.fields['test_program']):
                    self.assertNotIn(secret, text)

    def test_missing_domain_defaults_to_strict_class_with_domain_config(self):
        self.fields.pop('data_domain')
        self.fetch()
        self.assertEqual(self.provenance()['data_domain'], 'CLASS')
        self.assertEqual(self.provenance()['parameter_binding'], 'bound')

    def test_legacy_commands_only_allowed_without_explicit_domain(self):
        write_settings(self.root, settings())
        self.fields.pop('data_domain')
        self.parameters['CLASS'].unlink()
        self.fetch()
        self.assertEqual(self.provenance()['parameter_binding'], 'unverified_legacy')
        self.assertIsNone(self.provenance()['parameters'])
        self.assertEqual(self.provenance()['files'][0]['validation'],
                         {'operation': 'unverified', 'data_domain': 'unverified'})
        self.destination = self.root / 'explicit'
        with self.assertRaisesRegex(aqua.AquaError, 'parameter file is missing'):
            self.fetch(data_domain='CLASS')
        self.assertEqual(self.run.call_count, 1)

    def test_explicit_domain_with_legacy_shape_can_bind_parameter_file(self):
        value = settings()
        value['arguments'] += ['{parameter_file}', '{operations}', '{data_domain}']
        write_settings(self.root, value)
        self.fetch(data_domain='SORT')
        self.assertEqual(self.run.call_args.args[0][-2:], ['7777,8888', 'SORT'])
        self.assertEqual(self.provenance()['parameter_binding'], 'bound')

    def test_frozen_copy_survives_source_changes_across_lots(self):
        raw = self.parameters['CLASS'].read_bytes()
        def cli(command, **kwargs):
            frozen = Path(command[-3])
            self.assertEqual(frozen.read_bytes(), raw)
            self.assertTrue(frozen.is_absolute())
            self.parameters['CLASS'].write_bytes(b'changed after staging')
            return successful_cli(command, **kwargs)
        self.run.side_effect = cli
        self.fetch(lots=['L1', 'L2'])
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(self.provenance()['parameters']['sha256'], hashlib.sha256(raw).hexdigest())

    def test_source_change_during_selection_is_rejected(self):
        original = aqua.parameter_definition
        def changed(root, domain):
            definition = original(root, domain)
            definition['path'].write_bytes(b'changed during selection')
            return definition
        with patch.object(aqua, 'parameter_definition', side_effect=changed):
            with self.assertRaisesRegex(aqua.AquaError, 'changed during selection'):
                self.fetch()
        self.run.assert_not_called()

    def test_modified_frozen_file_is_rejected(self):
        def cli(command, **kwargs):
            frozen = Path(command[-3])
            frozen.chmod(0o644)
            frozen.write_bytes(b'tampered')
            return successful_cli(command, **kwargs)
        self.run.side_effect = cli
        with self.assertRaisesRegex(aqua.AquaError, 'frozen parameter file changed'):
            self.fetch()
        self.assertFalse((self.destination / 'retrieval.json').exists())

    def test_nonempty_destination_and_duplicate_lots_are_rejected(self):
        with self.assertRaisesRegex(aqua.AquaError, 'must not be duplicated'):
            self.fetch(lots=['L1', 'L1'])
        self.destination.mkdir()
        existing = self.destination / 'unrelated.txt'
        existing.write_bytes(b'preserve')
        with self.assertRaisesRegex(aqua.AquaError, 'destination must be fresh'):
            self.fetch()
        self.assertEqual(existing.read_bytes(), b'preserve')
        self.run.assert_not_called()

    def test_csv_metadata_validated_when_present_otherwise_unverified(self):
        for index, (metadata, expected) in enumerate((
                ({'Operation': '6248', 'Data Domain': 'CLASS'}, ('validated', 'validated')),
                ({'OPERATION_ID': '6248', 'DATA_DOMAIN': 'CLASS'}, ('validated', 'validated')),
                ({'operations': '6248', 'Domain': 'CLASS'}, ('validated', 'validated')),
                ({'Operation': '6248'}, ('validated', 'unverified')),
                ({'DataDomain': 'CLASS'}, ('unverified', 'validated')),
                ({}, ('unverified', 'unverified')))):
            with self.subTest(metadata=metadata):
                self.destination = self.root / f'metadata-{index}'
                def cli(command, **kwargs):
                    write_metadata_csv(output_path(command), metadata)
                    return subprocess.CompletedProcess(command, 0)
                self.run.side_effect = cli
                self.fetch()
                self.assertEqual(self.provenance()['files'][0]['validation'],
                                 dict(zip(('operation', 'data_domain'), expected)))

    def test_csv_wrong_blank_or_mixed_metadata_rejected_even_on_unselected_tests(self):
        for index, metadata in enumerate(({'Operation': '7777'}, {'Operation': ''},
                {'Operation': '6248,7777'}, {'Operation': '6248.0'}, {'Data Domain': 'SORT'},
                {'DATA_DOMAIN': ''}, {'Operation': '6248', 'operation_id': '7777'})):
            with self.subTest(metadata=metadata):
                self.destination = self.root / f'bad-metadata-{index}'
                def cli(command, **kwargs):
                    path = output_path(command)
                    write_metadata_csv(path, metadata, test='UNCONFIGURED')
                    return subprocess.CompletedProcess(command, 0)
                self.run.side_effect = cli
                with self.assertRaisesRegex(aqua.AquaError, 'missing or unexpected (operation|data domain)'):
                    self.fetch()
                self.assertFalse((self.destination / 'retrieval.json').exists())
        self.destination = self.root / 'mixed-metadata'
        def mixed(command, **kwargs):
            path = output_path(command)
            write_metadata_csv(path, {'Operation': '6248', 'Data Domain': 'CLASS'})
            with path.open('a', encoding='utf-8', newline='') as stream:
                csv.writer(stream).writerow(['UNCONFIGURED', '21', 'L1', '7777', 'SORT'])
            return subprocess.CompletedProcess(command, 0)
        self.run.side_effect = mixed
        with self.assertRaisesRegex(aqua.AquaError, 'unexpected operation'):
            self.fetch()

    def test_sort_csv_accepts_each_selected_operation(self):
        def cli(command, **kwargs):
            path = output_path(command)
            write_metadata_csv(path, {'Operation': '7777', 'Data Domain': 'SORT'})
            with path.open('a', encoding='utf-8', newline='') as stream:
                csv.writer(stream).writerow(['DTS_S1', '22', 'L1', '8888', 'SORT'])
            return subprocess.CompletedProcess(command, 0)
        self.run.side_effect = cli
        self.fetch(data_domain='SORT')
        self.assertEqual(self.provenance()['files'][0]['validation'],
                         {'operation': 'validated', 'data_domain': 'validated'})


class DefaultConnectorIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Compose the existing Flask fixture without inheriting/re-running its tests.
        self.fixture = dashboard_tests.SharedDashboardTests(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.fixture.write_named_configuration()
        self.parameters = {domain: write_parameters(self.fixture.root, domain)
                   for domain in ('CLASS', 'SORT')}
        write_settings(self.fixture.root, domain_settings())
        hook = patch.object(profiles, "AQUA_TRACE_FETCHER", None)
        hook.start()
        self.addCleanup(hook.stop)
        runner = patch.object(aqua.subprocess, "run", side_effect=self.cli)
        self.run = runner.start()
        self.addCleanup(runner.stop)

    @staticmethod
    def cli(command, **kwargs):
        write_csv(output_path(command), test='IPC::ARR_SHARED::TEMP_DTS')
        with output_path(command).open('a', encoding='utf-8', newline='') as stream:
            csv.writer(stream).writerow(['IPC::PTH_DTS_MODULE::CALIBRATION', 'sensor_a:12.5|sensor_b:21',
                                         'fresh-unit', 'L1', 'Prog1', 'P1'])
        return subprocess.CompletedProcess(command, 0)

    def test_new_connector_success_and_failed_pull_preserves_old_report(self):
        fixture = self.fixture
        self.assertIsNone(profiles.AQUA_TRACE_FETCHER)
        slug = fixture.create(source="aqua")["slug"]
        stale = fixture.write_input(slug, [("SHARED", "999|999", "stale", "L1", "Prog1", "P1")])
        stale_before = stale.read_bytes()
        response = fixture.process(slug, pull=True)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        original = response.get_json()
        self.run.assert_called_once()
        fixture.fetcher.assert_not_called()
        self.assertEqual(original["counts"]["readings"], 4)
        self.assertTrue(original["snapshot"]["pull"])
        self.assertEqual(len(fixture.calls), 3)
        for call in fixture.calls:
            self.assertEqual({row["Unit_ID"] for row in call["rows"]}, {"fresh-unit"})
            self.assertEqual({row["Measurement_Point"] for row in call["rows"]}, {"sensor_a", "sensor_b"})
            self.assertEqual({row["Value"] for row in call["rows"]}, {12.5, 21.0})
        report_bytes = {}
        for url in original["reports"].values():
            with fixture.client.get(url) as report:
                self.assertEqual(report.status_code, 200)
                report_bytes[url] = report.data
        index = fixture.publish / "index.html"
        index_before = index.read_bytes()
        state = fixture.root / "Saved_Sessions" / "profiles" / slug
        manifest_before = (state / "manifest.json").read_bytes()
        versions_before = {path.name for path in (state / "versions").iterdir()}
        fixture.export_mock.reset_mock()
        failures = (subprocess.CompletedProcess([], 5), subprocess.TimeoutExpired("mock-cli", 37),
                    "missing", "wrong-lot")
        for failure in failures:
            with self.subTest(failure=str(failure)):
                def failed(command, **kwargs):
                    if isinstance(failure, Exception):
                        raise failure
                    if failure == "wrong-lot":
                        write_csv(output_path(command), lot="L2", test="SHARED")
                    return failure if isinstance(failure, subprocess.CompletedProcess) else subprocess.CompletedProcess(command, 0)
                self.run.side_effect = failed
                response = fixture.process(slug, pull=True)
                self.assertEqual(response.status_code, 502, response.get_data(as_text=True))
                self.assertEqual(fixture.client.get(f"/api/profiles/{slug}").get_json()["snapshot"], original["snapshot"])
                self.assertEqual((state / "manifest.json").read_bytes(), manifest_before)
                self.assertEqual({path.name for path in (state / "versions").iterdir()}, versions_before)
                self.assertEqual(index.read_bytes(), index_before)
                self.assertEqual(stale.read_bytes(), stale_before)
                self.assertFalse(list(state.glob(".fetch-*")))
                for url, content in report_bytes.items():
                    with fixture.client.get(url) as report:
                        self.assertEqual(report.status_code, 200)
                        self.assertEqual(report.data, content)
        fixture.export_mock.assert_not_called()

    def test_default_connector_program_only_and_trace_rejected(self):
        for source, lots, status, message in (("aqua", [], 502, "Program-only"),
                                              ("trace", ["L1"], 503, "TRACE")):
            with self.subTest(source=source):
                slug = self.fixture.create(source=source, lots=lots)["slug"]
                response = self.fixture.process(slug, pull=True)
                self.assertEqual(response.status_code, status)
                self.assertIn(message, response.get_json()["error"])
        self.run.assert_not_called()
        self.fixture.export_mock.assert_not_called()
        self.fixture.fetcher.assert_not_called()

    def test_library_domain_setup_status_is_independent_and_offline(self):
        fixture = self.fixture
        expected = {domain: {'parameter_file': path.name, 'ready': True,
                            'category_tests': {'thermal_diode': [], 'dts': [], 'dtsmon': []},
                            'operations': ['6248'] if domain == 'CLASS' else ['7777', '8888']}
                    for domain, path in self.parameters.items()}
        self.assertEqual(fixture.client.get('/api/profiles').get_json()['data_domains'], expected)
        self.parameters['SORT'].write_bytes(self.parameters['CLASS'].read_bytes())
        library = fixture.client.get('/api/profiles').get_json()
        self.assertEqual(library['data_domains']['CLASS'], expected['CLASS'])
        status = library['data_domains']['SORT']
        self.assertFalse(status['ready'])
        self.assertEqual(status['parameter_file'], self.parameters['SORT'].name)
        self.assertIn('Data Domain mismatch', status['error'])
        self.assertNotIn('operations', status)
        dashboard_tests.shared.publish_portal(fixture.root, fixture.publish)
        static = fixture.portal_data((fixture.publish / 'index.html').read_text(encoding='utf-8'))
        self.assertEqual(static['data_domains'], library['data_domains'])
        self.run.assert_not_called()
        fixture.fetcher.assert_not_called()

    def test_selected_invalid_sort_blocks_pull_and_preserves_class_snapshot(self):
        fixture = self.fixture
        slug = fixture.create(source='aqua', subflow_step='CLASSHOT')['slug']
        stale = fixture.write_input(slug)
        stale_before = stale.read_bytes()
        response = fixture.process(slug, pull=True)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        original = response.get_json()['snapshot']
        self.assertEqual(original['data_domain'], 'CLASS')
        state = fixture.root / 'Saved_Sessions' / 'profiles' / slug
        manifest = (state / 'manifest.json').read_bytes()
        versions = {path.name for path in (state / 'versions').iterdir()}
        published = {path.relative_to(fixture.publish): path.read_bytes()
                     for path in fixture.publish.rglob('*') if path.is_file()}
        profile_path = fixture.root / 'Profiles' / f'{slug}.json'
        saved_profile = profile_path.read_bytes()
        self.run.reset_mock()
        fixture.export_mock.reset_mock()
        for case, message in (('missing', 'parameter file is missing'),
                              ('class-content', 'Data Domain mismatch'),
                              ('malformed', 'valid @ Operations block')):
            with self.subTest(case=case):
                path = self.parameters['SORT']
                if case == 'missing':
                    path.unlink()
                else:
                    path.write_bytes(self.parameters['CLASS'].read_bytes()
                                     if case == 'class-content' else b'@ Operations : invalid')
                response = fixture.process(slug, pull=True, data_domain='SORT')
                self.assertEqual(response.status_code, 503, response.get_data(as_text=True))
                self.assertIn(message, response.get_json()['error'])
                self.assertEqual(fixture.client.get(f'/api/profiles/{slug}').get_json()['snapshot'], original)
                self.assertEqual((state / 'manifest.json').read_bytes(), manifest)
                self.assertEqual({path.name for path in (state / 'versions').iterdir()}, versions)
                self.assertEqual({path.relative_to(fixture.publish): path.read_bytes()
                                  for path in fixture.publish.rglob('*') if path.is_file()}, published)
                self.assertEqual(profile_path.read_bytes(), saved_profile)
                self.assertEqual(stale.read_bytes(), stale_before)
                self.assertFalse(list(state.glob('.fetch-*')))
        self.run.assert_not_called()
        fixture.fetcher.assert_not_called()
        fixture.export_mock.assert_not_called()

    def test_frozen_parameters_and_retrieval_survive_successful_version_publication(self):
        fixture = self.fixture
        fetch_profile = aqua.fetch_profile
        for domain in ('CLASS', 'SORT'):
            with self.subTest(domain=domain):
                raw = self.parameters[domain].read_bytes()
                staged = {}

                def cli(command, **kwargs):
                    frozen = Path(command[-3])
                    self.assertEqual(frozen.name, self.parameters[domain].name)
                    self.assertEqual(frozen.read_bytes(), raw)
                    self.assertFalse(frozen.stat().st_mode & stat.S_IWRITE)
                    self.assertEqual(command[-2], domain)
                    self.assertEqual(command[-1], '6248' if domain == 'CLASS' else '7777,8888')
                    self.assertEqual(command[command.index('-step') + 1], 'CLASSHOT')
                    # The successful version must use the frozen copy, not reread this file.
                    self.parameters[domain].write_bytes(b'changed after retrieval was staged')
                    return self.cli(command, **kwargs)

                def fetch(root, fields, destination, tests):
                    files = fetch_profile(root, fields, destination, tests)
                    staged['folder'] = destination
                    staged['retrieval'] = json.loads((destination / 'retrieval.json').read_text(encoding='utf-8'))
                    return files

                self.run.reset_mock()
                self.run.side_effect = cli
                slug = fixture.create(source='aqua', subflow_step='CLASSHOT')['slug']
                with patch.object(aqua, 'fetch_profile', side_effect=fetch):
                    response = fixture.process(slug, pull=True, data_domain=domain)
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                snapshot = response.get_json()['snapshot']
                self.assertEqual(snapshot['data_domain'], domain)
                self.assertEqual(snapshot['profile']['data_domain'], domain)
                self.assertEqual(snapshot['profile']['source'], 'aqua')
                self.assertEqual(snapshot['profile']['subflow_step'], 'CLASSHOT')
                self.assertEqual(snapshot['retrieval'], staged['retrieval'])
                self.assertEqual(snapshot['retrieval']['parameter_binding'], 'bound')
                self.assertEqual(snapshot['retrieval']['parameters'], {
                    'filename': self.parameters[domain].name,
                    'sha256': hashlib.sha256(raw).hexdigest(),
                    'operations': ['6248'] if domain == 'CLASS' else ['7777', '8888'],
                    'data_domain': domain})
                self.assertEqual(snapshot['parameter_file'], 'parameters/' + self.parameters[domain].name)
                self.assertEqual([source['path'] for source in snapshot['sources']], ['0-aqua_L1.csv'])
                self.assertEqual(response.get_json()['counts']['readings'], 4)
                state = fixture.root / 'Saved_Sessions' / 'profiles' / slug
                for folder in (state / 'versions' / snapshot['version'],
                               fixture.publish / 'reports' / slug / snapshot['version']):
                    self.assertEqual((folder / snapshot['parameter_file']).read_bytes(), raw)
                    self.assertEqual(json.loads((folder / 'retrieval.json').read_text()), staged['retrieval'])
                    self.assertEqual(json.loads((folder / 'snapshot.json').read_text()), snapshot)
                self.assertFalse(staged['folder'].exists())
                self.assertFalse(list(state.glob('.fetch-*')))
                self.run.assert_called_once()
                fixture.fetcher.assert_not_called()

    def test_domain_binding_uses_lot_priority_and_only_ingests_csv(self):
        fixture = self.fixture
        write_parameters(fixture.root, 'CLASS')
        value = domain_settings()
        value['domains']['CLASS']['arguments'].remove('-program')
        value['domains']['CLASS']['arguments'].remove('{test_program}')
        write_settings(fixture.root, value)
        def cli(command, **kwargs):
            self.assertNotIn('-program', command)
            self.assertTrue(Path(command[-3]).is_file())
            self.assertEqual(command[-2:], ['CLASS', '6248'])
            self.cli(command, **kwargs)
            with output_path(command).open('a', encoding='utf-8', newline='') as stream:
                csv.writer(stream).writerow(['IPC::ARR_SHARED::TEMP_DTS', 'sensor:999', 'other-program-unit',
                                             'L1', 'OtherProgram', 'P1'])
            return subprocess.CompletedProcess(command, 0)
        self.run.side_effect = cli
        slug = fixture.create(source='aqua')['slug']
        response = fixture.process(slug, pull=True)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['counts']['readings'], 5)
        self.run.assert_called_once()
        self.assertEqual({row['Unit_ID'] for call in fixture.calls for row in call['rows']},
                         {'fresh-unit', 'other-program-unit'})
        for call in fixture.calls:
            self.assertNotIn('Program_Name', call['filters'])
        state = fixture.root / 'Saved_Sessions' / 'profiles' / slug
        self.assertFalse(list(state.glob('.fetch-*')))


if __name__ == "__main__":
    unittest.main()