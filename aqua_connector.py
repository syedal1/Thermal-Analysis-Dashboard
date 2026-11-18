"""Retrieve AQUA sensor exports through MCP or an explicitly configured CLI."""

import hashlib
import json
import math
from fnmatch import fnmatchcase
import os
import re
import string
import subprocess
import uuid
from pathlib import Path

from large_data_store import RECIPE_FIELDS, _source_rows


class AquaError(ValueError):
    pass


PARAMETER_FILES = {
    'CLASS': 'NVL_Aqua_Class_Automation_Parameters.txt',
    'SORT': 'NVL_Aqua_Sort_Automation_Parameters.txt',
}


def _domain(value):
    if not isinstance(value, str) or value not in PARAMETER_FILES:
        raise AquaError('AQUA data_domain must be CLASS or SORT.')
    return value


def parameter_definition(root, data_domain='CLASS'):
    """Resolve a fixed local AQUA export and verify its operation/domain metadata."""
    domain = _domain(data_domain)
    path = Path(root).resolve() / 'config' / PARAMETER_FILES[domain]
    if path.is_symlink() or not path.is_file():
        raise AquaError(f'AQUA {domain} parameter file is missing or is not a trusted regular file.')
    try:
        raw = path.read_bytes()
        lines = raw.decode('utf-8-sig').splitlines()
    except (OSError, UnicodeError) as error:
        raise AquaError(f'Cannot read AQUA {domain} parameter file.') from error
    starts = [i for i, line in enumerate(lines) if re.match(r'\s*@\s+Operations\b', line)]
    if len(starts) != 1 or not re.fullmatch(r'\s*@\s+Operations\s*:\s*', lines[starts[0]]):
        raise AquaError('AQUA parameter file requires exactly one valid @ Operations block.')
    operations = []
    source_domains = set()
    for line in lines[starts[0] + 1:]:
        line = line.strip()
        if re.match(r'@(?!@)', line):
            break
        if not line or line.startswith('#'):
            continue
        if operations and re.fullmatch(r'@@@\s+Selected Filters\s*:\s*\S.*', line):
            continue
        match = re.fullmatch(r'@@\s+([0-9]+)\|\s*:\s*(.+);\s*', line)
        if not match:
            raise AquaError('Malformed AQUA operation metadata in @ Operations block.')
        operation, metadata = match.groups()
        attributes = {}
        for item in metadata.split(';'):
            key, separator, value = item.strip().partition('=')
            key, value = key.strip(), value.strip()
            if not separator or not key or not value or key in attributes:
                raise AquaError('Malformed AQUA operation metadata in @ Operations block.')
            attributes[key] = value
        actual = attributes.get('Data Domain')
        allowed = {'CLASS'} if domain == 'CLASS' else {'SORT', 'SDX'}
        if actual not in allowed:
            raise AquaError(f'AQUA {domain} parameter file Data Domain mismatch: expected {domain}; '
                            'the export must declare the requested domain for every operation.')
        if actual == 'SDX' and not any(re.fullmatch(r'\s*@\s+Domain\s*:\s*PreAssembly\s*', line)
                                       for line in lines):
            raise AquaError('AQUA SORT/SDX parameter export must declare @ Domain : PreAssembly.')
        source_domains.add(actual)
        if len(source_domains) != 1:
            raise AquaError('AQUA parameter export must not mix source data domains.')
        if operation in operations:
            raise AquaError('Duplicate AQUA operation metadata in @ Operations block.')
        operations.append(operation)
    if not operations:
        raise AquaError('AQUA @ Operations block contains no operations.')
    definition = {'path': path, 'sha256': hashlib.sha256(raw).hexdigest(),
                  'operations': operations, 'data_domain': domain}
    source_domain = source_domains.pop()
    if source_domain != domain:
        definition['source_data_domain'] = source_domain
    return definition


def load_settings(root, data_domain=None):
    """Select domain settings; only omitted-domain, non-domain commands are legacy.

    Callers creating new profiles must persist an explicit data_domain and pass it
    here for preflight as well as to fetch_profile. Server reports own their filters;
    the local parameter export is only an expected-domain reference in that mode.
    """
    domain = _domain('CLASS' if data_domain is None else data_domain)
    path = Path(root) / 'config' / 'aqua_cli.json'
    if not path.is_file():
        raise AquaError('AQUA CLI is not configured: add config/aqua_cli.json with executable, '
                        'report_path and arguments from a verified sensor-report command. '
                        'See config/DTS_PLOT_WORKFLOW.md for cache and saved-report workflows.')
    settings = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(settings, dict):
        raise AquaError('AQUA CLI configuration must be an object.')
    if settings.get('transport', 'cli') not in ('cli', 'mcp'):
        raise AquaError('AQUA transport must be mcp or cli.')
    # This key is internal, never an administrator-provided definition/path.
    settings.pop('_parameter_definition', None)
    strict = data_domain is not None or 'domains' in settings
    if 'domains' in settings:
        domains = settings['domains']
        if not isinstance(domains, dict) or any(key not in PARAMETER_FILES for key in domains):
            raise AquaError('AQUA domains must be an object keyed by CLASS or SORT.')
        selected = domains.get(domain)
        if not isinstance(selected, dict):
            raise AquaError(f'AQUA domains configuration requires a {domain} object.')
        if any(key not in {'report_path', 'arguments', 'mode', 'cache_directory'} for key in selected):
            raise AquaError('AQUA domain settings support only report_path, arguments, mode and cache_directory; '
                            'executable, server and timeout_seconds are common settings.')
        settings = {key: value for key, value in settings.items() if key != 'domains'}
        # Never fall back to a different domain or a common saved report/command.
        settings.update({key: selected.get(key) for key in ('report_path', 'arguments')})
        settings.update({key: selected[key] for key in ('mode', 'cache_directory') if key in selected})
    definition = parameter_definition(root, domain) if strict else None
    export_mode = settings.get('mode', 'saved_report') in ('parameter_export', 'report_config')
    server_report_mode = settings.get('mode') == 'server_report'
    if settings.get('mode', 'saved_report') not in ('saved_report', 'parameter_export', 'server_report', 'report_config'):
        raise AquaError('AQUA mode must be saved_report, parameter_export, server_report or report_config.')
    if server_report_mode:
        definition = definition or parameter_definition(root, domain)
        strict = True
        report = settings.get('report_path') or os.environ.get('AQUA_REPORT_PATH', '')
        if not isinstance(report, str) or not report.strip():
            raise AquaError('AQUA server_report mode requires report_path or AQUA_REPORT_PATH '
                            'pointing to a saved sensor report on the AQUA server.')
        settings['report_path'] = report.strip()
    if export_mode:
        definition = definition or parameter_definition(root, domain)
        strict = True
        if settings.get('mode') == 'parameter_export':
            cache = settings.get('cache_directory')
            if not isinstance(cache, str) or not cache.strip() or not Path(os.path.expandvars(cache)).is_absolute():
                raise AquaError('AQUA parameter_export mode requires an absolute server-accessible cache_directory.')
        settings['report_path'] = 'staged-parameter-export'
    if export_mode or server_report_mode:
        report_argument = '-ReportConfig' if settings.get('mode') == 'report_config' else '-reportPath'
        settings['arguments'] = ['-aquaServer', '{server}', report_argument, '{report_path}',
                                 '-lots', '{lot}', '-outputFileName', '{output}']
    for key in ('executable', 'report_path', 'arguments'):
        if not settings.get(key):
            raise AquaError(f'AQUA CLI configuration requires {key}.')
    if not isinstance(settings['arguments'], list) or not all(isinstance(a, str) for a in settings['arguments']):
        raise AquaError('AQUA arguments must be a list of separate command arguments, not a shell command.')
    if not all(isinstance(settings[k], str) for k in ('executable', 'report_path')):
        raise AquaError('AQUA executable and report_path must be strings.')
    placeholders = set()
    for argument in settings['arguments']:
        try:
            for _, field, spec, conversion in string.Formatter().parse(argument):
                if field is not None:
                    if field not in {'lot', 'output', 'report_path', 'server', 'test_program',
                                     'subflow_step', 'product', 'parameter_file', 'data_domain', 'operations'} or spec or conversion:
                        raise AquaError('Unsupported AQUA argument placeholder.')
                    placeholders.add(field)
        except ValueError as error:
            raise AquaError('Invalid AQUA argument template.') from error
    if not {'lot', 'output', 'report_path'} <= placeholders:
        raise AquaError('AQUA arguments must include {lot}, {output} and {report_path}.')
    strict = strict or bool(placeholders & {'parameter_file', 'data_domain', 'operations'})
    if strict:
        if not export_mode and not server_report_mode and 'parameter_file' not in placeholders:
            raise AquaError('AQUA domain arguments must include {parameter_file} from a verified CLI command.')
        definition = definition or parameter_definition(root, domain)
        report_name = settings['report_path'].replace('\\', '/').rsplit('/', 1)[-1].lower()
        if report_name in {name.lower() for name in PARAMETER_FILES.values()} or '{' in settings['report_path']:
            raise AquaError('AQUA report_path must be a saved report, not a parameter file or placeholder.')
        for index, argument in enumerate(settings['arguments']):
            previous = settings['arguments'][index - 1].lower() if index else ''
            if ('{parameter_file}' in argument and (previous == '-reportpath'
                    or argument.lower().startswith(('-reportpath=', '-reportpath:')))):
                raise AquaError('AQUA parameter_file cannot be passed as reportPath; it is not a saved report.')
        settings['data_domain'] = domain
        settings['_parameter_definition'] = definition
    timeout = settings.get('timeout_seconds', 3600)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 14400:
        raise AquaError('AQUA timeout_seconds must be between 1 and 14400.')
    if settings.get('server', 'GAR') not in ('GAR', 'AMR', 'GER'):
        raise AquaError('AQUA server must be GAR, AMR or GER.')
    return settings


def _validate_download(path, lot, tests, data_domain=None, operations=None, program=None):
    valid = False
    has_rows = False
    verified = {'operation': False, 'data_domain': False}
    for row in _source_rows(path):
        has_rows = True
        actual_lot = str(row.get('Lot', row.get('LOTS', ''))).strip()
        allowed = {lot} if isinstance(lot, str) else lot
        if allowed is not None and actual_lot not in allowed:
            raise AquaError('Downloaded CSV contains a missing or unexpected lot; report was not accepted.')
        if program and not fnmatchcase(str(row.get('Program_Name') or row.get('Program Name') or ''), program):
            raise AquaError('Downloaded CSV contains a missing or unexpected test program.')
        if data_domain is not None:
            # Check all rows, including tests that will later be excluded by SQL.
            for key, value in row.items():
                header = re.sub(r'[\s_]+', '', str(key)).lower()
                if header in {'operation', 'operations', 'operationid'}:
                    if str(value or '').strip() not in (operations or ()):
                        raise AquaError('Downloaded CSV contains a missing or unexpected operation.')
                    verified['operation'] = True
                elif header in {'domain', 'datadomain'}:
                    if str(value or '').strip() != data_domain:
                        raise AquaError('Downloaded CSV contains a missing or unexpected data domain.')
                    verified['data_domain'] = True
        if not any(fnmatchcase(str(row.get('TEST_NAME') or ''), pattern) for pattern in tests):
            continue
        result = str(row.get('TEST_RESULT') or '')
        if not result.strip():
            result = str(row.get('TEST_RESULT_NUMERIC', ''))
        for token in result.split('|'):
            try:
                value = float(token.split(':', 1)[-1].strip())
            except ValueError:
                continue
            valid = valid or math.isfinite(value)
    if not has_rows or not valid:
        raise AquaError('AQUA returned no finite sensor readings for configured tests. '
                        'UNITS_LIST metadata is not a sensor report.')
    return {key: 'validated' if value else 'unverified' for key, value in verified.items()}


def _verify_frozen(path, expected_hash):
    if (path.is_symlink() or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash):
        raise AquaError('AQUA frozen parameter file changed; download was not accepted.')


def _sensor_report(raw, tests):
    text = raw.decode('utf-8-sig')
    if not re.search(r'(?m)^@ Report\s*:\s*\S', text):
        raise AquaError('AQUA parameter export must contain a complete @ Report definition.')
    blocks = re.split(r'(?m)(?=^@ Parameters Group\s*:|^@ Report Format\b|^@ Post Processing Definition)', text)
    output = []
    matched = set()
    for block in blocks:
        group = re.match(r'@ Parameters Group\s*:\s*([^\r\n]+)', block)
        if not group or group[1].strip() == 'Session Data':
            output.append(block)
            continue
        if group[1].strip() not in ('Parametric', 'String'):
            continue
        selected = []
        has_selected = False
        selected_index = 0
        for line in block.splitlines(keepends=True):
            record = re.match(r'\s*@@\s+"[0-9]+\|([^"]+)"\s*:', line)
            if record:
                covered = {test for test in tests if fnmatchcase(test, record[1])}
                if not covered:
                    continue
                matched.update(covered)
                has_selected = True
                for test in sorted(covered):
                    selected.append(re.sub(r'@@\s+"[0-9]+\|[^"]+"',
                                           lambda match: f'@@ "{selected_index}|{test}"', line, count=1))
                    selected_index += 1
                continue
            selected.append(line)
        if has_selected:
            output.append(''.join(selected))
    if not matched:
        raise AquaError('No configured sensor tests match the AQUA parameter export.')
    return ''.join(output).encode('utf-8'), sorted(matched)


def fetch_profile(root, fields, destination, tests):
    if fields.get('source') != 'aqua':
        raise AquaError('TRACE retrieval is not configured; the reference describes TRACE MCP metadata, not a sensor CLI.')
    domain = _domain(fields.get('data_domain', 'CLASS'))
    settings = load_settings(root, domain if 'data_domain' in fields else None)
    definition = settings.get('_parameter_definition')
    lots = fields.get('lots') or []
    if not isinstance(lots, list) or any(not isinstance(lot, str) or not re.fullmatch(r'[A-Za-z0-9]+', lot) for lot in lots):
        raise AquaError('AQUA lot IDs must be alphanumeric.')
    program = '' if lots else str(fields.get('test_program') or '').strip()
    if not lots and (not program or not re.fullmatch(r'[A-Za-z0-9_.*?-]+', program) or not re.search(r'[A-Za-z0-9]', program)):
        raise AquaError('Provide lot IDs or an exact/wildcard test program name.')
    if program and settings.get('mode') not in ('report_config', 'parameter_export'):
        raise AquaError('Program-only retrieval requires report_config or parameter_export mode.')
    if len(set(lots)) != len(lots):
        raise AquaError('AQUA lot IDs must not be duplicated.')
    executable = Path(os.path.expandvars(settings['executable']))
    if not executable.is_absolute() or executable.suffix.lower() != '.exe':
        raise AquaError('AQUA executable must be an absolute path to the approved CLI executable.')
    destination = Path(destination)
    if destination.is_symlink() or (destination.exists() and
                                    (not destination.is_dir() or any(destination.iterdir()))):
        raise AquaError('AQUA destination must be fresh; existing downloads will not be reused.')
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    frozen = None
    provenance = {'data_domain': domain, 'parameter_binding': 'unverified_legacy',
                  'parameters': None, 'files': []}
    if definition:
        raw = definition['path'].read_bytes()
        if hashlib.sha256(raw).hexdigest() != definition['sha256']:
            raise AquaError('AQUA parameter file changed during selection; retry with a stable export.')
        frozen = destination / definition['path'].name
        with frozen.open('xb') as stream:
            stream.write(raw)
        frozen.chmod(0o444)
        provenance.update(parameter_binding='unverified_server_report' if settings.get('mode') == 'server_report' else 'bound', parameters={
            'filename': frozen.name, 'sha256': definition['sha256'],
            'operations': definition['operations'], 'data_domain': domain})
        if 'source_data_domain' in definition:
            provenance['source_data_domain'] = definition['source_data_domain']
            provenance['parameters']['source_data_domain'] = definition['source_data_domain']
    report_path = settings['report_path']
    staged_report = None
    if settings.get('mode') == 'server_report':
        provenance['report'] = {'path': report_path, 'mode': 'server_report',
                                'filters': 'server_owned', 'subflow_step': 'unverified'}
    if settings.get('mode') in ('parameter_export', 'report_config'):
        original_report = frozen.read_bytes()
        report_bytes, report_tests = _sensor_report(original_report, set(tests) | {RECIPE_FIELDS[domain]})
        if not set(report_tests).intersection(tests):
            raise AquaError('No configured sensor tests match the AQUA parameter export.')
        complete_export = definition.get('source_data_domain') == 'SDX'
        if complete_export:
            report_bytes = original_report
        report_bytes = _query_report(report_bytes, lots, program)
        cache = destination if settings['mode'] == 'report_config' else Path(os.path.expandvars(settings['cache_directory']))
        staged_report = cache / f'thermal_sensors_{uuid.uuid4().hex}.txt'
        try:
            cache.mkdir(parents=True, exist_ok=True)
            with staged_report.open('xb') as stream:
                stream.write(report_bytes)
        except OSError as error:
            if settings['mode'] == 'report_config':
                raise AquaError('Cannot write the local AQUA report configuration in the download folder.') from error
            raise AquaError('Cannot stage the AQUA report in the server cache; use an authorized writable '
                            'cache_directory, or select server_report mode with report_path / AQUA_REPORT_PATH. '
                            'See config/DTS_PLOT_WORKFLOW.md.') from error
        report_path = str(staged_report)
        provenance['report'] = {'path': report_path, 'sha256': hashlib.sha256(report_bytes).hexdigest(),
                                'tests': report_tests, 'mode': settings['mode']}
        if complete_export:
            provenance['report']['selection'] = 'complete_export'
            provenance['report']['configured_tests'] = provenance['report'].pop('tests')
    files = []
    batch_size = max(1, int(settings.get('lot_batch_size', 50)))
    batches = [lots[index:index + batch_size] for index in range(0, len(lots), batch_size)] or [None]
    for number, batch in enumerate(batches, 1):
        lot = ','.join(batch) if batch else None
        name = batch[0] if batch and len(batch) == 1 else (f'batch{number}' if batch else 'program')
        output = destination / f'aqua_{name}.csv'
        if output.exists():
            raise AquaError('AQUA destination must be fresh; existing downloads will not be reused.')
        values = {key: fields.get(key, '') for key in ('test_program', 'subflow_step', 'product')}
        values.update(test_program=program, lot=lot or '', output=str(output), report_path=report_path, server=settings.get('server', 'GAR'))
        if frozen:
            _verify_frozen(frozen, definition['sha256'])
            values.update(parameter_file=str(frozen), data_domain=definition.get('source_data_domain', domain),
                          operations=','.join(definition['operations']))
        command = [str(executable), *[argument.format_map(values) for argument in settings['arguments']]]
        if program:
            for index in range(len(command) - 2, 0, -1):
                if command[index].lower() == '-lots' and command[index + 1] == '':
                    del command[index:index + 2]
            command.extend(['-programNames', program])
        if staged_report:
            _verify_frozen(staged_report, provenance['report']['sha256'])
        try:
            if settings.get('transport', 'cli') == 'mcp':
                from mcp_connector import execute_aqua

                if settings.get('mode') != 'report_config':
                    raise AquaError('AQUA MCP requires report_config mode.')
                arguments = {'aquaserver': settings.get('server', 'GAR'),
                             'outputfilename': str(output), 'ReportConfig': report_path}
                if lot:
                    arguments['lots'] = lot
                else:
                    arguments['programNames'] = program
                response = execute_aqua(root, arguments, settings.get('timeout_seconds', 3600))
                if response.get('errorMessage'):
                    raise AquaError(response['errorMessage'])
                provenance['transport'] = 'AQUA MCP'
                result = subprocess.CompletedProcess(command, 0)
            else:
                result = subprocess.run(command, cwd=destination, shell=False, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        timeout=settings.get('timeout_seconds', 3600), check=False)
                provenance['transport'] = 'AQUA CLI'
        except subprocess.TimeoutExpired as error:
            raise AquaError('AQUA download timed out; no previous data was used.') from error
        except OSError as error:
            raise AquaError('Cannot launch the configured AQUA CLI. Check installation, network access and permissions.') from error
        if frozen:
            _verify_frozen(frozen, definition['sha256'])
        if staged_report:
            _verify_frozen(staged_report, provenance['report']['sha256'])
        if result.returncode != 0:
            raise AquaError(f'AQUA download failed with exit code {result.returncode}; no previous data was used.')
        if not output.is_file() or output.stat().st_size == 0:
            raise AquaError('AQUA did not create the requested CSV. Verify the configured output argument.')
        try:
            validation = _validate_download(output, set(batch) if batch else None, tests,
                                            definition.get('source_data_domain', domain) if definition else None,
                                            definition['operations'] if definition else None, program=program)
        except (ValueError, UnicodeError) as error:
            raise AquaError(f'AQUA sensor CSV validation failed: {error}') from error
        files.append(output)
        provenance['files'].append({'filename': output.name, 'validation': validation})
    # The caller receives CSVs only; this sidecar must not be ingested as readings.
    with (destination / 'retrieval.json').open('x', encoding='utf-8') as stream:
        json.dump(provenance, stream, indent=2)
    return files


def _query_report(raw, lots, program):
    text = raw.decode('utf-8-sig')
    if not re.search(r'(?m)^@ Attributes\s*:', text):
        return raw
    newline = '\r\n' if '\r\n' in text else '\n'
    text = re.sub(r'(?m)^[ \t]*@@ (?:Lot|ProgramName|LastNWwsStartDateTime)\s*:[\s\S]*?^[ \t]*;[ \t]*(?:\r?\n|$)', '', text)
    name, values = ('Lot', lots) if lots else ('ProgramName', [program])
    attribute = newline.join([f'\t@@ {name} :', *[f'\t\t = {value}' for value in values], '\t;']) + newline
    text = re.sub(r'(?m)^@ Attributes[^\r\n]*\r?\n', lambda match: match[0] + attribute, text, count=1)
    if lots:
        text = re.sub(r'(?m)^[ \t]*@@@ Selected Filters : ProgramName[ \t]*\r?\n', '', text)
    return text.encode('utf-8')