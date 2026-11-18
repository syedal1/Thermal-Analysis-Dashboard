"""MCP transports owned by the local dashboard, independent of a chat session."""

import asyncio
import csv
import hashlib
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@asynccontextmanager
async def connection(root, server_name, timeout=3600):
    config = json.loads((Path(root) / '.vscode' / 'mcp.json').read_text(encoding='utf-8-sig'))
    server = config['servers'][server_name]
    if server.get('type') != 'stdio':
        raise ValueError('The dashboard requires a configured stdio MCP server.')
    parameters = StdioServerParameters(
        command=server['command'], args=server.get('args', []),
        cwd=server.get('cwd', str(Path(root).resolve())),
        env={**os.environ, **server.get('env', {})})
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=timeout)) as session:
            await session.initialize()
            yield session


def result_data(result):
    text = '\n'.join(item.text for item in result.content if item.type == 'text')
    if result.isError:
        raise ValueError(text or 'MCP tool failed without a message.')
    if result.structuredContent is not None:
        return result.structuredContent
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {'message': text}


def execute_aqua(root, arguments, timeout=3600):
    async def execute():
        async with asyncio.timeout(timeout):
            async with connection(root, 'aqua-mcp-server-gar', timeout) as session:
                return result_data(await session.call_tool('execute_aqua_command', arguments))
    return run_request(execute())


def run_request(request):
    try:
        return asyncio.run(request)
    except ExceptionGroup as error:
        pending = list(error.exceptions)
        messages = []
        while pending:
            item = pending.pop(0)
            if isinstance(item, BaseExceptionGroup):
                pending.extend(item.exceptions)
            else:
                messages.append(str(item) or type(item).__name__)
        raise ValueError('; '.join(messages)) from error
    except TimeoutError as error:
        raise ValueError('MCP retrieval timed out; no previous data was used.') from error


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


async def trace_tool(session, endpoint, tool, arguments, destination):
    request_path = destination / (uuid.uuid4().hex + '-request.json')
    request_path.write_text(json.dumps(arguments), encoding='utf-8')
    for retry in range(2):
        process = await asyncio.create_subprocess_exec(
            endpoint['cliPath'], 'invoke', '--endpoint', endpoint['endpoint'],
            '--tool', tool, '--args', '@' + str(request_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            async with asyncio.timeout(3600):
                stdout, stderr = await process.communicate()
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode == 3 and retry == 0:
            endpoint.update(result_data(await session.call_tool('get_cli_endpoint', {})))
            continue
        if process.returncode:
            raise ValueError(stderr.decode('utf-8', errors='replace').strip() or
                             f'{tool} failed with exit code {process.returncode}.')
        if not stdout.strip():
            raise ValueError(f'{tool} returned no response, not an empty result.')
        result = json.loads(stdout.decode('utf-8-sig'))
        request_path.with_name(request_path.name.replace('-request', '-response')).write_bytes(stdout)
        if result.get('errorMessage'):
            raise ValueError(result['errorMessage'])
        if result.get('truncated'):
            raise ValueError(f'{tool} returned a truncated response; no partial snapshot was accepted.')
        return result


def normalize_trace(raw, output, metadata, tests, discover_dtsmon=True):
    from fnmatch import fnmatchcase
    from large_data_store import RECIPE_FIELDS, _normalize_header, _socket_value
    from shared_dashboard import _is_dtsmon_test

    count = 0
    with Path(raw).open(encoding='utf-8-sig', newline='') as source, output.open('x', encoding='utf-8', newline='') as target:
        reader = csv.DictReader(source)
        required = {'JobName', 'TestName', 'ResultType', 'VisualId', 'Value'}
        if not required <= set(reader.fieldnames or []):
            raise ValueError('TRACE CSV is missing required result columns.')
        writer = csv.writer(target)
        writer.writerow(['TEST_NAME', 'TEST_RESULT', 'VISUAL_ID', 'Lot', 'Program_Name',
                         'Product_Name', 'Socket', 'PARAMETER_GROUP', 'Unit Tester Id',
                         'Thermal Head Id', 'Unit Tester Site Id'])
        for row in reader:
            if row['JobName'] != metadata['name']:
                raise ValueError('TRACE CSV contains an unexpected job.')
            name = row['TestName']
            if not any(fnmatchcase(name, pattern) for pattern in tests) and not (discover_dtsmon and _is_dtsmon_test(name)) and name not in RECIPE_FIELDS.values():
                continue
            if row['ResultType'] not in ('String', 'Parametric'):
                continue
            equipment = {_normalize_header(key): value for key, value in row.items()}
            testers = metadata.get('testers')
            tester = equipment.get('Unit Tester Id') or (
                testers[0] if isinstance(testers, list) and len(testers) == 1 else '')
            socket = _socket_value(equipment) or _socket_value({'ProcessStep': metadata.get('processStep')})
            writer.writerow([name, row['Value'], row['VisualId'], metadata['lot'],
                             metadata.get('programName') or '', metadata.get('partType') or '',
                             socket, row['ResultType'].upper(), tester,
                             equipment.get('Thermal Head Id') or '', equipment.get('Unit Tester Site Id') or ''])
            count += 1
    return count


def fetch_trace(root, fields, destination, tests):
    from aqua_connector import parameter_definition
    from large_data_store import RECIPE_FIELDS

    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError('TRACE destination must be fresh.')
    definition = parameter_definition(root, fields['data_domain'])
    lots = fields.get('lots') or []
    if not lots:
        raise ValueError('TRACE fallback requires explicit lot IDs.')
    settings_path = Path(root) / 'config' / 'trace_connector.json'
    settings = json.loads(settings_path.read_text(encoding='utf-8-sig')) if settings_path.is_file() else {}
    domain_settings = settings.get(fields['data_domain'], {})
    filters = dict(domain_settings, lotNames=','.join(lots), operation=','.join(definition['operations']),
                   mostRecent=False, includeSummary=True)
    filters.setdefault('type', 'Class' if fields['data_domain'] == 'CLASS' else 'Sort')
    filters.pop('testProgramNames', None)
    steps = fields.get('subflows') or [step.strip() for step in fields.get('subflow_step', '').split(',') if step.strip()]
    if steps and fields['data_domain'] == 'CLASS':
        filters['processStep'] = ','.join(steps)

    async def retrieve():
        async with connection(root, 'trace-mcp') as session:
            endpoint = result_data(await session.call_tool('get_cli_endpoint', {}))
            found = await trace_tool(session, endpoint, 'find_jobs', {'filter': filters}, destination)
            jobs = found.get('jobs') or []
            if not jobs:
                raise ValueError(found.get('message') or 'TRACE found no matching jobs.')
            if found.get('totalCount', len(jobs)) > len(jobs):
                raise ValueError('TRACE job results are incomplete; narrow the report query.')
            files, used_jobs, warnings = [], [], []
            if found.get('warning'):
                warnings.append(found['warning'])
            discover_dtsmon = ('dtsmon' in fields.get('analysis_categories', ['dtsmon'])
                              and not fields.get('selected_tests', {}).get('dtsmon'))
            queries = {name.split('*', 1)[0].split('?', 1)[0] for name in tests
                       if not (discover_dtsmon and '*' in name)}
            if '' in queries:
                raise ValueError('TRACE test patterns require a nonempty literal prefix.')
            queries = sorted(queries | {RECIPE_FIELDS[fields['data_domain']]} | ({'DTS'} if discover_dtsmon else set()))
            queries = [name for name in queries if not any(
                other != name and other.casefold() in name.casefold() for other in queries)]
            for job in jobs:
                details = await trace_tool(session, endpoint, 'get_detailed_jobs', {'jobs': [job]}, destination)
                records = details.get('classJobs', []) + details.get('sortJobs', []) + details.get('dbJobs', [])
                if len(records) != 1 or records[0]['name'] != job['name']:
                    raise ValueError('TRACE did not resolve the requested job metadata.')
                metadata = records[0]
                if metadata['lot'] not in lots or metadata['operation'] not in definition['operations']:
                    continue
                if steps and fields['data_domain'] == 'CLASS' and metadata.get('processStep') not in steps:
                    continue
                result = await trace_tool(session, endpoint, 'get_test_results',
                                          {'jobs': [job], 'testInstanceNames': queries}, destination)
                if result.get('warning'):
                    warnings.append(result['warning'])
                raw = result.get('filePath')
                if not raw:
                    if result.get('totalCount', 0):
                        raise ValueError('TRACE results have no complete CSV export.')
                    continue
                output = destination / f'trace-{len(files)}.csv'
                count = normalize_trace(raw, output, metadata, tests, discover_dtsmon=discover_dtsmon)
                if count:
                    files.append(output)
                    used_jobs.append(job)
                else:
                    output.unlink()
            if not files:
                raise ValueError('TRACE returned no matching thermal results for this report.')
            receipt = {'source': 'trace', 'transport': 'TRACE MCP / trace-cli',
                       'retrieved_at': datetime.now(timezone.utc).isoformat(),
                       'profile': fields, 'jobs': used_jobs, 'warnings': warnings,
                       'data_domain': fields['data_domain'],
                       'files': [{'filename': path.name, 'sha256': file_hash(path)}
                                 for path in files]}
            (destination / 'retrieval.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
            return files
    return run_request(retrieve())