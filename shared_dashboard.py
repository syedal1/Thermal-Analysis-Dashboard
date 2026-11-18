"""Local report-library service and explicit, offline static publisher.

WARNING: No authentication is provided. Owner is a label, not a verified identity.
The CLI binds to loopback; protect any wider deployment with authentication and
TLS at a trusted proxy. Cross-origin browser mutations are deliberately rejected,
including those from a file:// portal. Open the live service to make changes.

The two-tab UI is supplied by assets/profile_portal.html (__PORTAL_DATA__).
Processing publishes immutable report trees and atomically switches a local
manifest and refreshes the static library index. Metadata
requests read JSON only, never DuckDB. Old versions and user files are retained.
After a crash, an administrator must verify no worker remains before removing a
stale .shared_dashboard.lock; locks are never stolen automatically.

Lots take priority over exact/wildcard programs. Selected subflows filter actual
socket metadata. Processing freezes the dashboard product mapping.
Empty categories have null reports; no matched readings is an error.
"""

import argparse
import copy
import hashlib
import inspect
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
from fnmatch import fnmatchcase
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request, send_file
from werkzeug.exceptions import HTTPException


CATEGORIES = ("thermal_diode", "dts", "dtsmon")
DTSMON_PATTERNS = tuple(f'{scope}::{kind}_*::*_DTS{suffix}' for scope in ('IPC', 'IPH', 'IPG')
                       for kind in ('ARR', 'FUN', 'SCN')
                       for suffix in (('', '_1', '_2') if scope == 'IPC' else ('',)))
CATEGORY_FILES = {"thermal_diode.json": "thermal_diode", "dts.json": "dts",
                  "dtsmon.json": "dtsmon"}
PROFILE_FIELDS = {
    "owner", "work_week", "product", "test_program", "lots", "notes",
    "subflow_step", "source", "data_domain",
    "subflows", "analysis_categories", "selected_tests",
}
MAX_BODY = 64 * 1024
SLUG_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}\Z")
VERSION_PATTERN = re.compile(r"[a-f0-9]{32}\Z")
POWERPOINT_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
CATEGORY_TITLES = {"thermal_diode": "Thermal Diode", "dts": "DTS", "dtsmon": "DTSMON"}
VALIDATION_NOTE = (
    "Subflow/operation metadata not verified; manual exports must already be scoped appropriately. "
    "TRACE preflight not performed."
)


class SharedDashboardError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _safe_path(base, *parts):
    """Reject traversal and symlinks/junctions escaping the approved root."""
    base = Path(base).resolve()
    candidate = base.joinpath(*parts).resolve()
    if not candidate.is_relative_to(base):
        raise SharedDashboardError("Unsafe path.")
    return candidate


def _slug(value):
    if not isinstance(value, str) or not SLUG_PATTERN.fullmatch(value):
        raise SharedDashboardError("Invalid report slug.")
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", value, re.I):
        raise SharedDashboardError("Invalid report slug.")
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def _decode(value):
    return json.loads(value, object_pairs_hook=_pairs, parse_constant=_invalid_constant)


def _read_json(path):
    return _decode(path.read_text(encoding="utf-8-sig"))


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2).encode("utf-8")


def _html_json(value):
    return _json_bytes(value).decode("utf-8").replace("<", "\\u003c").replace(
        ">", "\\u003e").replace("&", "\\u0026")


def _atomic_bytes(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path, value):
    _atomic_bytes(path, _json_bytes(value))


@contextmanager
def _file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex.encode("ascii")
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SharedDashboardError("Processing or publishing is already in progress.", 409) from None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        # Never remove a lock that was acquired by somebody else.
        try:
            if not path.is_symlink() and path.read_bytes() == token:
                path.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _locked(root, publish_folder):
    paths = {_safe_path(root, ".shared_dashboard.lock"),
             _safe_path(publish_folder, ".shared_dashboard.lock")}
    with ExitStack() as stack:
        for path in sorted(paths, key=str):
            stack.enter_context(_file_lock(path))
        yield


def _text(body, field, maximum, required=False):
    value = body.get(field, "")
    if not isinstance(value, str) or len(value) > maximum:
        raise SharedDashboardError(f"{field} must be a string of at most {maximum} characters.")
    if any((ord(char) < 32 and (field != "notes" or char not in "\r\n\t"))
           or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise SharedDashboardError(f"{field} contains invalid characters.")
    value = value.strip()
    if required and not value:
        raise SharedDashboardError(f"{field} is required.")
    return value


def _profile_fields(body, legacy=False):
    if not isinstance(body, dict) or (not legacy and set(body) - PROFILE_FIELDS):
        raise SharedDashboardError("Expected a report object containing only supported fields.")
    result = {key: _text(body, key, size, key in {"owner", "work_week"} and not legacy)
              for key, size in {"owner": 120, "work_week": 64, "product": 120,
                                "test_program": 512, "notes": 4000, "subflow_step": 256}.items()}
    if not legacy and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]*", result["work_week"])
                       or ".." in result["work_week"]):
        raise SharedDashboardError("work_week must be an alphanumeric work-week label.")
    lots = body.get("lots", [])
    if isinstance(lots, str):
        if len(lots) > 16000:
            raise SharedDashboardError("lots is too long.")
        lots = [lot.strip() for lot in lots.split(",") if lot.strip()]
    if not isinstance(lots, list) or len(lots) > 100:
        raise SharedDashboardError("lots must be an array or comma-separated string (maximum 100 lots).")
    result["lots"] = [_text({"lot": lot}, "lot", 128, True) for lot in lots]
    if len(set(result["lots"])) != len(result["lots"]):
        raise SharedDashboardError("Duplicate lots are not allowed.")
    if not result["lots"] and not result["test_program"]:
        raise SharedDashboardError("At least one lot or test_program is required.")
    result["source"] = body.get("source", "manual" if legacy else "aqua")
    if result["source"] not in ("manual", "aqua", "trace"):
        raise SharedDashboardError("source must be manual, aqua or trace.")
    result['data_domain'] = body.get('data_domain', 'CLASS')
    if result['data_domain'] not in ('CLASS', 'SORT'):
        raise SharedDashboardError('data_domain must be CLASS or SORT.')
    categories = body.get('analysis_categories', list(CATEGORIES))
    if not isinstance(categories, list) or not categories or any(item not in CATEGORIES for item in categories):
        raise SharedDashboardError('Select at least one valid analysis category.')
    result['analysis_categories'] = list(dict.fromkeys(categories))
    subflows = body.get('subflows', [])
    if not isinstance(subflows, list) or any(item not in ('CLASSHOT', 'CSM', 'PHMHOT', 'PHMCOLD', 'PHMROOM') for item in subflows):
        raise SharedDashboardError('Select valid subflows.')
    result['subflows'] = list(dict.fromkeys(subflows))
    selected_tests = body.get('selected_tests', {})
    if not isinstance(selected_tests, dict) or set(selected_tests) - set(CATEGORIES):
        raise SharedDashboardError('Invalid selected tests.')
    for names in selected_tests.values():
        if not isinstance(names, list) or len(names) > 1000 or any(not isinstance(name, str) or not name or len(name) > 1024 for name in names):
            raise SharedDashboardError('Selected tests must be lists of test names.')
    result['selected_tests'] = selected_tests
    return result


def _selected_configuration(config, fields):
    selected = {}
    for name, record in config.items():
        categories = [key for key in record.get('CATEGORIES', [_category(name, record)]) if key in fields['analysis_categories']
                      and (not fields['selected_tests'].get(key) or any(fnmatchcase(name, pattern) for pattern in fields['selected_tests'][key]))]
        if categories:
            selected[name] = dict(record, CATEGORIES=categories)
    return selected


def _fetch_tests(config, fields):
    tests = set(config)
    if 'dtsmon' in fields['analysis_categories']:
        tests.update(fields['selected_tests'].get('dtsmon') or DTSMON_PATTERNS)
    return {name for name in tests if not any(other != name and fnmatchcase(name, other) for other in tests)}


def _owner_root(root):
    settings = _safe_path(root, 'config', 'owner_storage.json')
    return Path(_read_json(settings)['reports_root']).resolve() if settings.is_file() else _safe_path(root, 'reports')


def _extended_storage_path(path):
    path = Path(path).resolve()
    if os.name != 'nt' or str(path).startswith('\\\\?\\'):
        return path
    text = str(path)
    return Path('\\\\?\\UNC\\' + text[2:] if text.startswith('\\\\') else '\\\\?\\' + text)


def _owner_folder(root, owner):
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 _-]{0,119}', owner)
            or owner.upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{index}' for index in range(1, 10)), *(f'LPT{index}' for index in range(1, 10))}):
        raise SharedDashboardError('Owner must be a folder-safe name containing letters, digits, spaces, underscores or hyphens.')
    return _safe_path(_extended_storage_path(_owner_root(root)), owner)


def _register_owner(root, owner):
    folder = _owner_folder(root, owner)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if not (folder / 'owner.json').exists():
            _atomic_json(folder / 'owner.json', {'name': owner, 'created': _now()})
        elif _read_json(folder / 'owner.json')['name'] != owner:
            raise SharedDashboardError('An owner already exists with different capitalization.')
    except OSError as error:
        raise SharedDashboardError(f'Cannot access owner storage: {folder}. {error}', 503) from error
    return folder


def _save_owner_report(root, profile, snapshot=None, source_folder=None):
    destination = _safe_path(_register_owner(root, profile['owner']), _slug(profile['slug']))
    _atomic_json(destination / 'profile.json', profile)
    if snapshot:
        version = snapshot['version']
        target = _safe_path(destination, 'versions', version)
        if not target.exists():
            pending = _safe_path(destination, 'versions', '.pending-' + uuid.uuid4().hex)
            try:
                shutil.copytree(_extended_storage_path(_safe_path(_state_folder(root, profile['slug']), 'versions', version)), pending)
                if source_folder:
                    shutil.copytree(_extended_storage_path(source_folder), pending / 'input')
                pending.rename(target)
            finally:
                if pending.exists():
                    shutil.rmtree(pending)
        _atomic_json(destination / 'manifest.json', {'version': version})
    return str(_safe_path(_owner_root(root), profile['owner'], profile['slug']))


def _profile_path(root, slug):
    return _safe_path(root, "Profiles", f"{_slug(slug)}.json")


def _state_folder(root, slug):
    return _safe_path(root, "Saved_Sessions", "profiles", _slug(slug))


def _load_profile(root, slug):
    path = _profile_path(root, slug)
    if not path.is_file():
        raise SharedDashboardError("Report not found.", 404)
    try:
        profile = _read_json(path)
        if not isinstance(profile, dict):
            raise ValueError("Not an object")
    except (ValueError, UnicodeError) as error:
        raise SharedDashboardError("Invalid saved report JSON.", 422) from error
    profile["slug"] = slug  # The filename, never file content, selects storage.
    profile["owner"] = profile.get("owner") or "Legacy / unknown"
    return profile


def _snapshot(root, slug):
    state = _state_folder(root, slug)
    manifest = _safe_path(state, "manifest.json")
    if not manifest.is_file():
        return None
    pointer = _read_json(manifest)
    version = pointer.get("version") if isinstance(pointer, dict) else None
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise SharedDashboardError("Invalid report manifest.", 422)
    snapshot = _read_json(_safe_path(state, "versions", version, "snapshot.json"))
    if not isinstance(snapshot, dict) or snapshot.get("version") != version:
        raise SharedDashboardError("Invalid report snapshot.", 422)
    return snapshot


def _zero_counts():
    return dict.fromkeys(("readings", "products", "tests", "units", "lots"), 0)


def _report_relative(slug, version, relative):
    # Exported paths are always POSIX relative paths, including on Windows.
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or any(p in (".", "..") for p in path.parts) \
            or "\\" in relative or ":" in relative:
        raise SharedDashboardError("Unsafe report path.", 422)
    return f"reports/{slug}/{version}/{path.as_posix()}"


def _metadata(root, slug, static=False):
    profile = _load_profile(root, slug)
    snapshot = _snapshot(root, slug)
    reports = dict.fromkeys(CATEGORIES)
    powerpoints = dict.fromkeys(CATEGORIES)
    if snapshot:
        for category in CATEGORIES:
            relative = snapshot["reports"].get(category)
            if relative:
                reports[category] = ("" if static else "/") + _report_relative(
                    slug, snapshot["version"], relative)
            if _powerpoint_path(root, slug, snapshot["version"], category).is_file():
                powerpoints[category] = ("" if static else "/") + _powerpoint_relative(
                    slug, snapshot["version"], category)
    profile.update(
        reports=reports,
        powerpoints=powerpoints,
        category_tests=snapshot["category_tests"] if snapshot else {key: [] for key in CATEGORIES},
        available_tests=snapshot.get("available_tests", snapshot["category_tests"]) if snapshot else
        {key: [] for key in CATEGORIES},
        validation_note=snapshot.get("validation_note", VALIDATION_NOTE) if snapshot else VALIDATION_NOTE,
        counts=snapshot["counts"] if snapshot else _zero_counts(),
        category_counts=snapshot["category_counts"] if snapshot else {key: _zero_counts() for key in CATEGORIES},
        config_snapshot=snapshot["config_snapshot"] if snapshot else None,
        snapshot=snapshot,
    )
    job_path = _safe_path(_state_folder(root, slug), 'job.json')
    if not static and job_path.is_file():
        profile['job'] = _read_json(job_path)
    return profile


def _product_names(root):
    path = _safe_path(root, 'config', 'Product_Name.txt')
    products = {'CLASS': [], 'SORT': []}
    if not path.is_file():
        return products
    domain = None
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('@'):
            domain = line[1:].strip().upper()
            if domain not in products:
                raise SharedDashboardError('Product_Name.txt contains an invalid domain.', 422)
            continue
        match = re.fullmatch(r'\d+\.\s*(.+)', line)
        if domain is None or not match:
            raise SharedDashboardError('Product_Name.txt requires @ CLASS/@ SORT sections and numbered names.', 422)
        product = _text({'product': match[1]}, 'product', 120, True)
        if product not in products[domain]:
            products[domain].append(product)
    return products


def _validate_product(root, fields):
    if _safe_path(root, 'config', 'Product_Name.txt').is_file():
        products = _product_names(root)
        if fields['product'] not in products[fields['data_domain']]:
            raise SharedDashboardError('Select a product listed for this domain in config/Product_Name.txt.', 422)


def _library(root, service_url, static=False, requested_category=None):
    profiles = []
    for path in sorted(_safe_path(root, "Profiles").glob("*.json")):
        # Invalid hand-written/legacy files must not break the entire library.
        try:
            profiles.append(_metadata(root, _slug(path.stem), static))
        except (SharedDashboardError, OSError, ValueError, KeyError, TypeError):
            continue
    profiles.sort(key=lambda item: str(item.get("created", item.get("updated", ""))), reverse=True)
    result = {"profiles": profiles, "service_url": service_url, "generated_at": _now(), "live": not static}
    try:
        result['owners'] = sorted(set([item['owner'] for item in profiles if item.get('owner')] +
                                     [path.parent.name for path in _owner_root(root).glob('*/owner.json')]))
        result['owner_storage'] = str(_owner_root(root))
    except OSError as error:
        result['owners'] = sorted({item['owner'] for item in profiles if item.get('owner')})
        result['owner_storage_error'] = str(error)
    baseline = _baseline_slug(root, profiles)
    if baseline:
        result['baseline_slug'] = baseline
    try:
        result['product_names'] = _product_names(root)
    except (SharedDashboardError, OSError, UnicodeError) as error:
        result['product_names'] = {'CLASS': [], 'SORT': []}
        result['product_names_error'] = str(error)
    from aqua_connector import PARAMETER_FILES, parameter_definition, load_settings

    result['data_domains'] = {}
    for domain, filename in PARAMETER_FILES.items():
        status = {'parameter_file': filename, 'ready': False}
        try:
            definition = parameter_definition(root, domain)
            status['operations'] = definition['operations']
            if 'source_data_domain' in definition:
                status['source_data_domain'] = definition['source_data_domain']
            load_settings(root, domain)
            status['ready'] = True
        except (ValueError, OSError) as error:
            status['error'] = str(error)
        result['data_domains'][domain] = status
    try:
        config, _, _, files = _load_configuration(root)
        result["configuration"] = _configuration_metadata(config, files)
        for domain, status in result['data_domains'].items():
            try:
                definition = parameter_definition(root, domain)
                patterns = re.findall(r'@@\s+"[0-9]+\|([^"]+)"', Path(definition['path']).read_text(encoding='utf-8-sig'))
                status['category_tests'] = {key: sorted(name for name in names if any(fnmatchcase(name, pattern) for pattern in patterns))
                                            for key, names in result['configuration']['category_tests'].items()}
                status['category_tests']['dtsmon'] = sorted(set(status['category_tests']['dtsmon']) |
                                                           {pattern for pattern in patterns if _is_dtsmon_test(pattern)})
            except (ValueError, OSError):
                status['category_tests'] = result['configuration']['category_tests']
    except (SharedDashboardError, OSError, ValueError, TypeError, RecursionError) as error:
        result["configuration"] = {"category_files": {key: [] for key in CATEGORIES},
                                   "category_tests": {key: [] for key in CATEGORIES}}
        result["configuration_error"] = str(error)
    return result


def _baseline_slug(root, profiles):
    release_path = _safe_path(root, "release.json")
    if not release_path.is_file():
        return None
    try:
        release = _read_json(release_path)
        baseline = release.get("baseline_slug") if isinstance(release, dict) else None
        return baseline if any(profile["slug"] == baseline for profile in profiles) else None
    except (OSError, ValueError, UnicodeError, TypeError):
        return None


def _render(root, data):
    template = _safe_path(root, "assets", "profile_portal.html")
    if not template.is_file() and getattr(sys, 'frozen', False):
        template = Path(sys._MEIPASS) / "assets" / "profile_portal.html"
    if not template.is_file():
        raise SharedDashboardError("The report portal asset template is not installed.", 503)
    text = template.read_text(encoding="utf-8")
    if "__PORTAL_DATA__" not in text:
        raise SharedDashboardError("The portal template is missing __PORTAL_DATA__.", 503)
    return text.replace("__PORTAL_DATA__", _html_json(data))


def _category(test, record):
    explicit = record.get("CATEGORY")
    if explicit is not None and explicit != "":
        if not isinstance(explicit, str):
            raise SharedDashboardError("CATEGORY must be a string.", 422)
        value = re.sub(r"[ _-]", "", explicit).upper()
        aliases = {"THERMALDIODE": "thermal_diode", "DIODE": "thermal_diode",
                   "DTS": "dts", "DTSMON": "dtsmon"}
        if value not in aliases:
            raise SharedDashboardError(f"Unknown configured CATEGORY: {explicit}", 422)
        return aliases[value]
    name = test.upper()
    if any(token in name for token in ("DTSMON", "DTS_MON", "DTS-MON")):
        return "dtsmon"
    if "DIODE" in name:
        return "thermal_diode"
    return "dts" if "DTS" in name else None


def _configuration_metadata(config, files):
    """Only names, not sensor records or limits, belong in library metadata."""
    groups = {category: [] for category in CATEGORIES}
    category_files = {category: [] for category in CATEGORIES}
    for name, record in config.items():
        categories = record["CATEGORIES"] if "CATEGORIES" in record else [_category(name, record)]
        for category in categories:
            if category in groups:
                groups[category].append(name)
    for file in files:
        for category in file.get("categories", []):
            category_files[category].append(Path(file["path"]).name)
    return {"category_files": {key: sorted(set(names)) for key, names in category_files.items()},
            "category_tests": {key: sorted(set(names)) for key, names in groups.items()},
            "warnings": [warning for file in files for warning in file.get('warnings', [])]}


def _is_dtsmon_test(name):
    return re.fullmatch(
        r'(?:IPC::(?:ARR|FUN|SCN)_[^:]*::[^:]*_DTS(?:_[12])?'
        r'|IP[HG]::(?:ARR|FUN|SCN)_[^:]*::[^:]*_DTS)', name) is not None


def _group_dts_tests(config, discovered):
    grouped = copy.deepcopy(config)
    for name, record in list(grouped.items()):
        categories = record.get('CATEGORIES', [])
        if 'dtsmon' in categories and (
                'dts' in categories or name not in discovered or not _is_dtsmon_test(name)):
            record['CATEGORIES'] = [category for category in categories if category != 'dtsmon']
            if not record['CATEGORIES']:
                del grouped[name]
    for name, columns in discovered.items():
        if not _is_dtsmon_test(name):
            continue
        record = grouped.get(name, {})
        categories = record.get('CATEGORIES', [])
        if 'dts' not in categories:
            grouped[name] = dict(record, TEST_NAME=name,
                                 COLUMN_NAMES=record.get('COLUMN_NAMES', columns),
                                 CATEGORIES=sorted(set(categories + ['dtsmon'])),
                                 SOURCE_CONFIGS=sorted(set(record.get('SOURCE_CONFIGS', []) + ['DTSMON.json'])))
    return grouped


def _category_sensor_config(config, category):
    selected = copy.deepcopy(config)
    for record in selected.values():
        columns = record.get('CATEGORY_COLUMN_NAMES', {}).get(category)
        if columns is not None:
            record['COLUMN_NAMES'] = list(columns)
    return selected


def _discover_dtsmon_sensors(folder, config):
    from large_data_store import SUPPORTED_SUFFIXES, _reference_sensor_results, _source_rows

    selected = copy.deepcopy(config)
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.name.startswith('~$') or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        for row in _source_rows(path):
            name = row.get('TEST_NAME')
            if not isinstance(name, str) or not _is_dtsmon_test(name):
                continue
            if row.get('PARAMETER_GROUP') == 'PARAMETRIC' and 'TEST_RESULT_NUMERIC' in row:
                continue
            result = row.get('TEST_RESULT')
            if not isinstance(result, str) or ':' not in result or '=' in result:
                continue
            sensors = _reference_sensor_results(result, [])
            if not sensors:
                continue
            record = selected.setdefault(name, {'TEST_NAME': name, 'COLUMN_NAMES': [],
                                                'CATEGORIES': ['dtsmon'], 'SOURCE_CONFIGS': ['DTSMON.json']})
            if 'dts' in record['CATEGORIES']:
                continue
            record['COLUMN_NAMES'] = list(dict.fromkeys(record['COLUMN_NAMES'] + list(sensors)))
            record['RESULT_PARSER'] = 'dts_reference'
    return selected


def _load_configuration(root, requested_category=None):
    if requested_category is not None and requested_category not in CATEGORIES:
        raise SharedDashboardError("Invalid analysis category.")
    config = {}
    raw_files = {}
    metadata = []
    limits = {}
    folder = _safe_path(root, "config")
    try:
        candidates = sorted(path for path in folder.iterdir()
                            if path.suffix.lower() == ".json") if folder.exists() else []
        named = any(path.name.lower() in CATEGORY_FILES for path in candidates)
        selected_categories = ({'dts', 'dtsmon'} if requested_category in ('dts', 'dtsmon')
                               and any(path.name.lower() == 'dts.json' for path in candidates)
                               else {requested_category})
        for candidate in candidates:
            filename = candidate.name.lower()
            if requested_category is not None and filename in CATEGORY_FILES and CATEGORY_FILES[filename] not in selected_categories:
                continue
            if filename in ("aqua_cli.json", "owner_storage.json") or (named and filename not in CATEGORY_FILES
                                               and filename != "product_test_limits.json"):
                continue
            path = _safe_path(root, "config", candidate.name)
            raw = path.read_bytes()
            data = _decode(raw.decode("utf-8-sig"))
            file_categories = set()
            file_warnings = []
            if filename in CATEGORY_FILES:
                file_categories.add(CATEGORY_FILES[filename])
            if path.name.lower() == "product_test_limits.json":
                if not isinstance(data, dict):
                    raise ValueError("Product limits must be an object")
                limits = data
            else:
                if not isinstance(data, list):
                    raise ValueError(f"{path.name} must contain a list of test records")
                records = {}
                for record in data:
                    if not isinstance(record, dict):
                        raise ValueError("Test configuration records must be objects")
                    name = record.get('TEST_NAME')
                    if not isinstance(name, str) or not name.strip():
                        raise ValueError('TEST_NAME must be nonempty')
                    if name in records:
                        file_warnings.append(f'{path.name}: duplicate {name}; last definition is used.')
                    records[name] = record
                for record in records.values():
                    name = record.get("TEST_NAME")
                    if filename == 'dtsmon.json' and not _is_dtsmon_test(name):
                        file_warnings.append(f'{path.name}: excluded {name}; outside DTSMON test patterns.')
                        continue
                    columns = record.get("COLUMN_NAMES")
                    if not isinstance(name, str) or not name.strip():
                        raise ValueError("TEST_NAME must be nonempty")
                    if isinstance(columns, str):
                        columns = [column.strip() for column in columns.split(",")]
                    if not isinstance(columns, list) or not columns or any(
                            not isinstance(column, str) or not column.strip() for column in columns):
                        raise ValueError(f"Invalid COLUMN_NAMES for {name}")
                    normalized = dict(record, COLUMN_NAMES=[column.strip() for column in columns])
                    category = CATEGORY_FILES[filename] if named else _category(name, normalized)
                    if requested_category is not None and category not in selected_categories:
                        continue
                    # Filename membership is authoritative; legacy CATEGORY retains its semantics.
                    if named:
                        normalized.pop("CATEGORY", None)
                    normalized["CATEGORIES"] = [category] if category else []
                    normalized["SOURCE_CONFIGS"] = [path.name]
                    if named and category in ('dts', 'dtsmon'):
                        normalized['RESULT_PARSER'] = 'dts_reference'
                    if category:
                        file_categories.add(category)
                    if name in config:
                        previous = config[name]
                        if previous["COLUMN_NAMES"] != normalized["COLUMN_NAMES"]:
                            if not named or category in previous['CATEGORIES']:
                                raise ValueError(f"Incompatible COLUMN_NAMES for {name} in "
                                                 f"{', '.join(previous['SOURCE_CONFIGS'])} and {path.name}")
                            mappings = previous.setdefault('CATEGORY_COLUMN_NAMES', {
                                key: list(previous['COLUMN_NAMES']) for key in previous['CATEGORIES']})
                            mappings[category] = list(normalized['COLUMN_NAMES'])
                            if len(normalized['COLUMN_NAMES']) > len(previous['COLUMN_NAMES']):
                                previous['COLUMN_NAMES'] = list(normalized['COLUMN_NAMES'])
                        elif 'CATEGORY_COLUMN_NAMES' in previous:
                            previous['CATEGORY_COLUMN_NAMES'][category] = list(normalized['COLUMN_NAMES'])
                        ignored = {"CATEGORIES", "SOURCE_CONFIGS", 'COLUMN_NAMES',
                                   'CATEGORY_COLUMN_NAMES', 'RESULT_PARSER'}
                        if {key: value for key, value in previous.items() if key not in ignored} != {
                                key: value for key, value in normalized.items() if key not in ignored}:
                            raise ValueError(f"Conflicting configuration for {name}")
                        previous["CATEGORIES"] = sorted(set(previous["CATEGORIES"] + normalized["CATEGORIES"]))
                        previous["SOURCE_CONFIGS"] = sorted(set(previous["SOURCE_CONFIGS"] + [path.name]))
                        if normalized.get('RESULT_PARSER'):
                            previous['RESULT_PARSER'] = normalized['RESULT_PARSER']
                    else:
                        config[name] = normalized
            raw_files[path.name] = raw
            metadata.append({"path": f"config/{path.name}", "size": len(raw),
                             "sha256": hashlib.sha256(raw).hexdigest(),
                             "categories": sorted(file_categories), "warnings": file_warnings})
    except (ValueError, UnicodeError) as error:
        raise SharedDashboardError(f"Invalid configuration: {error}", 422) from error
    if not config:
        raise SharedDashboardError("No configured tests were found in root/config.", 422)
    return config, limits, raw_files, metadata


def _source_fingerprints(folder):
    from large_data_store import SUPPORTED_SUFFIXES

    result = []
    for candidate in sorted(folder.iterdir()):
        if candidate.name.startswith("~$") or candidate.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        path = _safe_path(folder, candidate.name)
        if not path.is_file():
            continue
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise SharedDashboardError("Input changed during processing; retry when writing finishes.", 409)
        result.append({"path": candidate.name, "size": after.st_size,
                       "mtime_ns": after.st_mtime_ns, "sha256": digest.hexdigest()})
    return result


def export_dashboard(store, output_folder, product=None, source_note=None, plot_limit=5000,
                     filters=None, sensor_config=None, limits=None, report_kind='dts', limit_product=None,
                     report_product=None, per_socket=False):
    """Lazy integration seam; never fall back to the old unfiltered exporter."""
    from export_dashboard_html import export_dashboard as exporter

    parameters = inspect.signature(exporter).parameters
    if not {"filters", "sensor_config", "limits"}.issubset(parameters):
        raise SharedDashboardError(
            "The dashboard exporter needs filters, sensor_config and limits support before processing.", 503)
    if report_kind in ('thermal_diode', 'dts', 'dtsmon'):
        from category_reports import export_diode, export_dts, export_dtsmon

        writer = {'thermal_diode': export_diode, 'dts': export_dts, 'dtsmon': export_dtsmon}[report_kind]
        selected = dict(filters or {})
        if product:
            selected['Product_Name'] = product
        options = {'limit_product': limit_product, 'report_product': report_product,
             'per_socket': per_socket} if report_kind in ('thermal_diode', 'dts') else {'per_socket': per_socket, 'group_by_hardware': True}
        return writer(store, output_folder, filters=selected, sensor_config=sensor_config,
                      limits=limits, source_note=source_note, plot_limit=plot_limit, **options)
    return exporter(store, output_folder, product=product, source_note=source_note,
                    plot_limit=plot_limit, filters=filters, sensor_config=sensor_config, limits=limits)


def _copy_version(root, publish_folder, slug, snapshot):
    version = snapshot["version"]
    source = _safe_path(_state_folder(root, slug), "versions", version)
    destination = _safe_path(publish_folder, "reports", slug, version)
    # Only a successfully generated version is eligible for publication.
    if _read_json(_safe_path(source, "snapshot.json")) != snapshot:
        raise SharedDashboardError("Report snapshot does not match its manifest.", 422)
    for relative in snapshot["reports"].values():
        if relative:
            _report_relative(slug, version, relative)
            if not _safe_path(source, relative).is_file():
                raise SharedDashboardError("A successful snapshot report is missing.", 422)
    for path in source.rglob("*"):
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise SharedDashboardError("Report trees must not contain links outside their version.", 422)
    if destination.exists():
        # Immutable versions are never overwritten, even if a user modified one.
        if _read_json(_safe_path(destination, "snapshot.json")) != snapshot:
            raise SharedDashboardError("Published version already exists with different metadata.", 409)
        for relative in snapshot["reports"].values():
            if relative and not _safe_path(destination, relative).is_file():
                raise SharedDashboardError("Published report is missing; existing user data was not overwritten.", 409)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _safe_path(destination.parent, f".pending-{uuid.uuid4().hex}")
    try:
        shutil.copytree(source, temporary)
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)  # Only this invocation's private staging tree.


def _unlinked_path(base, *parts):
    """Resolve within a trusted root, rejecting even in-root links/reparse points."""
    target = _safe_path(base, *parts)
    current = Path(base).resolve()
    for part in Path(*parts).parts:
        current = current / part
        if current.is_symlink() or (current.exists() and
                getattr(current.lstat(), "st_file_attributes", 0) & 0x400):
            raise SharedDashboardError("Linked export/report paths are not allowed.", 422)
    return target


def _powerpoint_relative(slug, version, category):
    _slug(slug)
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version) or category not in CATEGORIES:
        raise SharedDashboardError("Invalid PowerPoint path.", 404)
    return f"exports/{slug}/{version}/{category}.pptx"


def _powerpoint_path(root, slug, version, category):
    _powerpoint_relative(slug, version, category)
    return _unlinked_path(root, "Saved_Sessions", "profiles", slug, "exports", version, f"{category}.pptx")


def _powerpoint_source(root, slug, snapshot, category):
    relative = snapshot["reports"].get(category)
    if relative is None:
        raise SharedDashboardError("No HTML report is available for this category.", 404)
    if (not isinstance(relative, str) or not relative or
            any(not part or part.startswith(".") for part in relative.split("/"))):
        raise SharedDashboardError("Unsafe report path.", 422)
    _report_relative(slug, snapshot["version"], relative)
    if PurePosixPath(relative).suffix.lower() != ".html":
        raise SharedDashboardError("PowerPoint source must be a snapshot HTML report.", 422)
    source = _unlinked_path(root, "Saved_Sessions", "profiles", slug,
                            "versions", snapshot["version"], relative)
    if not source.is_file():
        raise SharedDashboardError("The committed category HTML report is missing.", 404)
    return source


def _atomic_copy(source, destination):
    """Bounded-memory copy; readers never see a partially copied deck."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with source.open("rb") as incoming, temporary.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_powerpoints(root, publish_folder, slug, snapshot):
    for category in CATEGORIES:
        source = _powerpoint_path(root, slug, snapshot["version"], category)
        if source.is_file():
            destination = _unlinked_path(publish_folder, _powerpoint_relative(
                slug, snapshot["version"], category))
            if not destination.exists():
                _atomic_copy(source, destination)
            elif not destination.is_file():
                raise SharedDashboardError("Published PowerPoint path is not a file.", 409)


def _generate_powerpoint(root, slug, body):
    """Caller holds _locked throughout snapshot selection, export and publication."""
    _load_profile(root, slug)
    snapshot = _snapshot(root, slug)
    if snapshot is None:
        raise SharedDashboardError("No committed snapshot; process the report first.", 409)
    if snapshot["version"] != body["version"] or snapshot.get("data_domain") != body["data_domain"]:
        raise SharedDashboardError("Snapshot version or data_domain is stale; reload the report.", 409)
    category = body["category"]
    source = _powerpoint_source(root, slug, snapshot, category)
    destination = _powerpoint_path(root, slug, snapshot["version"], category)
    if not destination.is_file():
        from powerpoint_export import PowerPointExportError, export_report_pptx

        context = snapshot["profile"]
        title = (f"{CATEGORY_TITLES[category]} | {context.get('work_week', 'Unknown week')} | "
                 f"{context.get('owner', 'Unknown owner')} | {snapshot['data_domain']}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Isolate partial output even when the exporter fails after writing.
            with TemporaryDirectory(prefix=".pending-", dir=destination.parent) as temporary:
                staged = Path(temporary) / destination.name
                template = _unlinked_path(root, "config", "PPT_Template.pptx")
                options = {"template_path": template} if template.is_file() else {}
                export_report_pptx(source, destination=staged, title=title, **options)
                staged = _unlinked_path(temporary, destination.name)
                if not staged.is_file():
                    raise PowerPointExportError("The exporter did not create a PowerPoint file.")
                with staged.open("r+b") as stream:
                    os.fsync(stream.fileno())
                os.replace(staged, destination)
        except PowerPointExportError as error:
            raise SharedDashboardError(
                f"PowerPoint export unavailable: {error} Existing HTML is unchanged. "
                "Check python-pptx, Plotly/Pillow, Kaleido and local Chrome, then retry.", 503) from error
    return {"url": "/" + _powerpoint_relative(slug, snapshot["version"], category)}


def _process(root, publish_folder, slug, pull, data_domain=None, category=None, progress=None):
    progress = progress or (lambda message: None)
    profile = _load_profile(root, slug)
    fields = _profile_fields(profile, legacy=True)
    fields["slug"] = slug
    if fields['lots']:
        fields['test_program'] = ''
    if data_domain is not None:
        if data_domain not in ('CLASS', 'SORT'):
            raise SharedDashboardError('data_domain must be CLASS or SORT.')
        fields['data_domain'] = data_domain
    folder = _safe_path(root, "input", "profiles", slug)
    if not pull:
        folder.mkdir(parents=True, exist_ok=True)
        progress('Processing saved input files and building reports.')
        return _process_files(root, publish_folder, slug, profile, fields, folder, pull, category=category)
    if fields["source"] not in ("aqua", "trace"):
        raise SharedDashboardError("Pull requires source aqua or trace; manual reports cannot pull.")
    import profiles

    fetcher = profiles.AQUA_TRACE_FETCHER
    setup_error = None
    if fetcher is None:
        if fields['source'] == 'aqua':
            from aqua_connector import AquaError, fetch_profile, load_settings

            try:
                load_settings(root, fields['data_domain'])
                config, _, _, _ = _load_configuration(root, category)
                config = _selected_configuration(config, fields)
                fetcher = lambda lots, program, product, destination: fetch_profile(
                    root, fields, destination, _fetch_tests(config, fields))
            except (AquaError, ValueError, OSError) as error:
                setup_error = error
    state = _state_folder(root, slug)
    state.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".fetch-", dir=state) as temporary:
        retrieval = None
        parameter_copy = None
        selected_folder = Path(temporary) / "selected"
        last_error = setup_error
        attempts = 3 if fields['source'] == 'aqua' and fetcher is not None else (1 if fetcher else 0)
        for attempt in range(1, attempts + 1):
            fetch_folder = Path(temporary) / f"aqua-{attempt}"
            attempt_selected = Path(temporary) / f"selected-{attempt}"
            fetch_folder.mkdir()
            scope = f"{len(fields['lots'])} lot(s)" if fields['lots'] else f"program {fields['test_program']}"
            progress(f'Retrieving {scope} from AQUA (attempt {attempt} of {attempts}).')
            try:
                retrieval, parameter_copy = _stage_connector_files(
                    fetcher, fields, fetch_folder, attempt_selected)
                selected_folder = attempt_selected
                last_error = None
                break
            except Exception as error:
                last_error = error
        if last_error is not None or attempts == 0:
            progress('AQUA unavailable; retrieving from TRACE.')
            try:
                trace_error = None
                if _safe_path(root, '.vscode', 'mcp.json').is_file():
                    from mcp_connector import fetch_trace

                    trace_folder = Path(temporary) / 'trace-live'
                    trace_selected = Path(temporary) / 'selected-trace'
                    try:
                        config, _, _, _ = _load_configuration(root, category)
                        config = _selected_configuration(config, fields)
                        trace_fetcher = lambda lots, program, product, destination: fetch_trace(
                            root, fields, destination, _fetch_tests(config, fields))
                        retrieval, parameter_copy = _stage_connector_files(
                            trace_fetcher, fields, trace_folder, trace_selected)
                        selected_folder = trace_selected
                    except Exception as error:
                        trace_error = error
                if retrieval is None or trace_error is not None:
                    try:
                        _, retrieval = _trace_fallback(root, fields, selected_folder)
                    except SharedDashboardError as error:
                        if trace_error is not None:
                            raise SharedDashboardError(f'TRACE live retrieval failed: {trace_error}; {error}', 502) from trace_error
                        raise
                    if trace_error is not None:
                        retrieval['live_trace_error'] = str(trace_error)
                retrieval["fallback_reason"] = (
                    "TRACE source selected" if fields['source'] == 'trace' else
                    "AQUA connector unavailable" if attempts == 0 else
                    "AQUA pull failed after three attempts")
                _atomic_json(selected_folder / "retrieval.json", retrieval)
            except SharedDashboardError as fallback_error:
                if attempts == 0 and last_error is not None:
                    raise SharedDashboardError(
                        f"No Aqua/Trace connector is configured: {last_error}; {fallback_error}", 503) from last_error
                if last_error is None:
                    raise SharedDashboardError(str(fallback_error), 503) from fallback_error
                raise SharedDashboardError(
                    f"AQUA pull failed after {attempts} attempt(s) ({str(last_error)[:300]}); {fallback_error}",
                    502) from last_error
        progress('Data retrieved. Processing readings and building reports.')
        return _process_files(root, publish_folder, slug, profile, fields, selected_folder, pull,
                              retrieval=retrieval, parameter_copy=parameter_copy, category=category)


def _process_files(root, publish_folder, slug, profile, fields, folder, pull, retrieval=None, parameter_copy=None,
                   category=None):
    from atlas_thermal_dashboard_optimized import PRODUCT_MAPPING
    from category_reports import resolve_limit_product
    from large_data_store import ThermalStore

    product_mapping = copy.deepcopy(PRODUCT_MAPPING)
    config, limits, raw_files, config_files = _load_configuration(root, category)
    selected_product = profile.get('product', '')
    report_product = selected_product if selected_product in _product_names(root)[fields['data_domain']] else None
    limit_product = resolve_limit_product(profile.get('product') or fields['product'], limits)
    if limit_product is None and selected_product:
        limit_product = resolve_limit_product(f"NVL_{fields['data_domain'].title()}_{selected_product}", limits)
    if limit_product and limit_product.casefold().startswith('nvl_class_') and fields['data_domain'] != 'CLASS':
        limit_product = None
    analysis_category = category
    sources = _source_fingerprints(folder)
    if not sources:
        raise SharedDashboardError("No supported input files were found for this report.", 422)
    named_dts = any(name.lower() == 'dts.json' for name in raw_files)
    if category != 'thermal_diode' and (named_dts or any(name.lower() == 'dtsmon.json' for name in raw_files)):
        config = _discover_dtsmon_sensors(folder, config)
    config = _selected_configuration(config, fields)
    base_filters = {}
    for key, column in (("lots", "Lot"), ("test_program", "Program_Name"), ("product", "Product_Name")):
        if fields[key] and key != 'test_program' and not (key == 'product' and fields[key] == report_product):
            base_filters[column] = fields[key]
    if fields['subflows']:
        base_filters['Socket'] = fields['subflows']
    configuration = _configuration_metadata(config, config_files)
    groups = configuration["category_tests"]
    configured = sorted({name for names in groups.values() for name in names})
    if not configured:
        raise SharedDashboardError("No configured category tests were found.", 422)
    version = uuid.uuid4().hex
    versions = _safe_path(_state_folder(root, slug), "versions")
    versions.mkdir(parents=True, exist_ok=True)
    staging = _safe_path(versions, f".pending-{version}")
    staging.mkdir()
    database = _safe_path(root, "Saved_Sessions", "profiles", f"{slug}.duckdb")
    # Guard the paths that DuckDB itself derives from its database name.
    _safe_path(root, str(database.with_suffix(".spill")))
    _safe_path(root, str(database) + ".wal")
    try:
        for name, raw in raw_files.items():
            _atomic_bytes(_safe_path(staging, "config", name), raw)
        _atomic_json(staging / "sensor_config.json", config)
        _atomic_json(staging / "limits.json", limits)
        _atomic_json(staging / "product_mapping.json", product_mapping)
        if parameter_copy:
            name, raw = parameter_copy
            _atomic_bytes(staging / 'parameters' / name, raw)
        if retrieval:
            _atomic_json(staging / 'retrieval.json', retrieval)
        store = ThermalStore(database)
        try:
            store.refresh(folder, config, product_mapping)
            if not fields['lots'] and fields['test_program']:
                programs = [name for name in store.values('Program_Name') if fnmatchcase(name, fields['test_program'])]
                if not programs:
                    raise SharedDashboardError('No matching exact/wildcard test program in the source data.', 422)
                base_filters['Program_Name'] = programs
            generated_dtsmon_config = None
            partition_dts = analysis_category != 'thermal_diode' and any(
                name.lower() in ('dts.json', 'dtsmon.json') for name in raw_files)
            if partition_dts:
                discovered = {name: store.values('Measurement_Point', dict(base_filters, Test=[name]))
                              for name in store.values('Test', base_filters)
                              if _is_dtsmon_test(name) and name in config}
                config = _group_dts_tests(config, discovered)
                config = _selected_configuration(config, fields)
                for file in config_files:
                    if Path(file['path']).name.lower() == 'dtsmon.json':
                        seed = Path(file['path']).name
                        _atomic_bytes(staging / 'config' / 'DTSMON.seed.json', raw_files[seed])
                        file.update(path='config/DTSMON.seed.json', categories=[])
                generated_dtsmon_config = 'config/DTSMON.json'
                _atomic_json(staging / generated_dtsmon_config, [
                    {'TEST_NAME': name, 'COLUMN_NAMES': record['COLUMN_NAMES']}
                    for name, record in sorted(config.items()) if 'dtsmon' in record['CATEGORIES']])
                generated_raw = (staging / generated_dtsmon_config).read_bytes()
                config_files.append({'path': generated_dtsmon_config, 'size': len(generated_raw),
                                     'sha256': hashlib.sha256(generated_raw).hexdigest(),
                                     'categories': ['dtsmon'], 'warnings': []})
                configuration = _configuration_metadata(config, config_files)
                groups = configuration['category_tests']
                configured = sorted({name for names in groups.values() for name in names})
                _atomic_json(staging / 'sensor_config.json', config)
            selected = dict(base_filters, Test=configured)
            counts = store.overview(selected)
            if not counts["readings"]:
                raise SharedDashboardError("No configured matching readings for this report's lot/program/product filters.", 422)
            available = set(store.values("Test", selected))
            available_tests = {category: [name for name in names if name in available]
                               for category, names in groups.items()}
            generated_dts_config = None
            if analysis_category in ('dts', 'dtsmon') and not named_dts:
                generated_dts = {name: dict(config[name], COLUMN_NAMES=store.values(
                    'Measurement_Point', dict(base_filters, Test=[name])))
                          for name in available_tests['dts']}
                generated_dts_config = 'config/DTS.generated.json'
                _atomic_json(staging / generated_dts_config, [
                    {'TEST_NAME': name, 'COLUMN_NAMES': record['COLUMN_NAMES']}
                    for name, record in generated_dts.items()])
                config.update(generated_dts)
                _atomic_json(staging / 'sensor_config.json', config)
            reports = dict.fromkeys(CATEGORIES)
            category_counts = {category: _zero_counts() for category in CATEGORIES}
            for category, tests in available_tests.items():
                if not tests:  # An empty Test list means no filter in ThermalStore.
                    continue
                filters = dict(base_filters, Test=tests)
                category_config = _category_sensor_config(config, category)
                if category_config != config:
                    store.refresh(folder, category_config, product_mapping)
                category_counts[category] = store.overview(filters)
                output = staging / category
                report_options = {'limit_product': limit_product, 'report_product': report_product,
                                  'per_socket': fields['data_domain'] == 'CLASS'} if category in ('thermal_diode', 'dts') else {
                                      'per_socket': fields['data_domain'] == 'CLASS'}
                report = Path(export_dashboard(
                    store, output, product=base_filters.get('Product_Name'),
                    source_note=f"{fields['work_week']} | {profile['owner']} | {fields['data_domain']} | {category} | {VALIDATION_NOTE}",
                    plot_limit=5000, filters=copy.deepcopy(filters),
                    sensor_config=category_config, limits=copy.deepcopy(limits), report_kind=category,
                    **report_options)).resolve()
                if category_config != config:
                    store.refresh(folder, config, product_mapping)
                if not report.is_relative_to(output.resolve()) or not report.is_file():
                    raise SharedDashboardError("Exporter did not create a report inside its version folder.", 500)
                reports[category] = report.relative_to(staging).as_posix()
        finally:
            store.close()
        if sources != _source_fingerprints(folder):
            raise SharedDashboardError("Input changed during processing; previous reports were preserved.", 409)
        snapshot = {
            "version": version, "generated_at": _now(), "profile": dict(fields, slug=slug),
            "analysis_category": analysis_category,
            "filters": dict(base_filters, Test=configured),
            "validation_note": VALIDATION_NOTE, "product_mapping": product_mapping,
            "data_domain": fields['data_domain'], "retrieval": retrieval,
            "limit_product": limit_product,
            "report_product": report_product,
            "parameter_file": ('parameters/' + parameter_copy[0]) if parameter_copy else None,
            "plot_limit": 5000, "pull": pull, "sources": sources, "counts": counts,
            "category_tests": groups, "category_counts": category_counts, "reports": reports,
            "available_tests": available_tests,
            "generated_dts_config": generated_dts_config,
            "generated_dtsmon_config": generated_dtsmon_config,
            "report_types": {'thermal_diode': 'Histogram, scatter, line and distribution views with full-data statistics', 'dts': 'DTS variability by Domain or Sensor with full-data sensor statistics',
                             'dtsmon': 'Unit/test DTSMax summaries and variability plots, separated by socket for CLASS'},
            "config_snapshot": {"files": config_files, "sensor_config": "sensor_config.json",
                                "category_files": configuration["category_files"],
                                "limits": "limits.json", "product_mapping": "product_mapping.json"},
        }
        _atomic_json(staging / "snapshot.json", snapshot)
        staging.rename(_safe_path(versions, version))
        _copy_version(root, publish_folder, slug, snapshot)
        # The only commit point: old metadata remains valid if any earlier work fails.
        if profile.get('owner_storage'):
            try:
                _save_owner_report(root, profile, snapshot, folder)
            except OSError as error:
                raise SharedDashboardError(f'Owner report could not be saved; previous snapshot retained: {error}', 503) from error
        _atomic_json(_safe_path(_state_folder(root, slug), "manifest.json"), {"version": version})
        return _metadata(root, slug)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _refresh_product_report(root, publish_folder, slug, previous, profile):
    from large_data_store import ThermalStore

    snapshot = _snapshot(root, slug)
    if not snapshot or not snapshot['reports'].get('thermal_diode'):
        return False
    if profile['product'] not in _product_names(root)[profile['data_domain']]:
        return False
    if snapshot.get('report_product') == profile['product']:
        return False
    query_keys = ('lots', 'test_program', 'data_domain', 'subflow_step', 'source',
                  'analysis_categories', 'selected_tests', 'subflows')
    if any(previous.get(key) != profile.get(key) for key in query_keys):
        return False
    if any(snapshot['reports'].get(key) for key in CATEGORIES if key != 'thermal_diode'):
        return False
    database = _safe_path(root, 'Saved_Sessions', 'profiles', f'{slug}.duckdb')
    if not database.is_file():
        return False
    store = ThermalStore(database)
    try:
        paths = [Path(row[0]).resolve() for row in store.connection.execute('SELECT path FROM sources').fetchall()]
    finally:
        store.close()
    folders = {path.parent for path in paths}
    if len(folders) != 1 or any(not path.is_relative_to(root.resolve()) for path in paths):
        return False
    folder = folders.pop()
    if not folder.is_dir() or _source_fingerprints(folder) != snapshot['sources']:
        return False
    fields = _profile_fields(profile, legacy=True)
    source_product = snapshot['filters'].get('Product_Name')
    if source_product:
        fields['product'] = source_product
    _process_files(root, publish_folder, slug, profile, fields, folder, False, category='thermal_diode')
    return True


def _service_url(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("service_url must be an HTTP(S) URL")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username \
            or parsed.password or parsed.query or parsed.fragment or any(char.isspace() for char in value):
        raise ValueError("service_url must be an HTTP(S) URL without credentials, query or fragment")
    # Accessing port also validates malformed/out-of-range explicit ports.
    if parsed.port == 0:
        raise ValueError("service_url must use a nonzero port")
    return value.rstrip("/")


def publish_portal(root: Path, publish_folder: Path, service_url=None):
    """Return index.html after copying only manifest-selected successful trees.

    The static index uses reports/... links, so it works from a file share without
    a service. A supplied service_url is informational, not a CORS/auth bypass.
    No input files, caches, abandoned versions, or unrelated output are copied.
    """
    root, publish_folder = Path(root).resolve(), Path(publish_folder).resolve()
    service_url = _service_url(service_url)
    with _locked(root, publish_folder):
        return _publish_portal_locked(root, publish_folder, service_url)


def _publish_portal_locked(root, publish_folder, service_url):
    data = _library(root, service_url, static=True, requested_category="thermal_diode")
    rendered = _render(root, data)
    for profile in data["profiles"]:
        if profile["snapshot"]:
            _copy_version(root, publish_folder, profile["slug"], profile["snapshot"])
            _copy_powerpoints(root, publish_folder, profile["slug"], profile["snapshot"])
    destination = _safe_path(publish_folder, "index.html")
    _atomic_bytes(destination, rendered.encode("utf-8"))
    return destination


def _auto_publish(root, publish_folder, service_url, result, committed):
    try:
        _publish_portal_locked(root, publish_folder, service_url)
    except Exception:
        result["warning"] = (
            f"{committed}, but static index publication failed. Shared readers may see an older index; "
            "retry with POST /api/publish."
        )
    return result


def _publish_completed(root, publish_folder, service_url, result):
    try:
        with _locked(root, publish_folder):
            return _auto_publish(root, publish_folder, service_url, result, 'Snapshot committed')
    except SharedDashboardError as error:
        if error.status != 409:
            raise
        result['warning'] = 'Snapshot committed. Static publication is busy; use Publish HTML to refresh the shared index.'
        return result


def _origin(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username \
                or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            return None
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


def _trace_fallback(root, fields, destination):
    fallback = _safe_path(root, "input", "profiles", fields["slug"], "trace_fallback")
    receipt = _safe_path(fallback, "retrieval.json")
    if not receipt.is_file() or receipt.is_symlink():
        raise SharedDashboardError("No staged TRACE MCP export is available for this report.")
    try:
        retrieval = _read_json(receipt)
        if not isinstance(retrieval, dict) or retrieval.get("source") != "trace" \
                or retrieval.get("transport") != "TRACE MCP staged export":
            raise ValueError("Invalid source metadata")
        retrieved_at = datetime.fromisoformat(retrieval["retrieved_at"].replace("Z", "+00:00"))
        age_seconds = (datetime.now(timezone.utc) - retrieved_at).total_seconds()
        if retrieved_at.tzinfo is None or age_seconds < -300 or age_seconds > 86400:
            raise ValueError("Export must be no more than 24 hours old")
        expected = {key: fields[key] for key in ("slug", "lots", "test_program", "product",
                                                  "subflow_step", "data_domain")}
        if retrieval.get("profile") != expected:
            raise ValueError("Export filters do not match the saved report")
        files = retrieval.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError("Export contains no files")
        destination.mkdir(parents=True, exist_ok=True)
        selected = []
        for index, record in enumerate(files):
            if not isinstance(record, dict):
                raise ValueError("Invalid file record")
            relative = record.get("path")
            if (not isinstance(relative, str) or "\\" in relative or ":" in relative
                    or Path(relative).is_absolute() or any(part in (".", "..") for part in relative.split("/"))):
                raise ValueError("Invalid relative file path")
            source = _safe_path(fallback, *relative.split("/"))
            if source.is_symlink() or not source.is_file() or source.suffix.lower() not in (".csv", ".tsv", ".txt"):
                raise ValueError("Unsupported or unsafe file")
            digest = hashlib.sha256()
            with source.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != record.get("sha256"):
                raise ValueError("File checksum mismatch")
            target = destination / f"trace-{index}-{source.name}"
            shutil.copy2(source, target)
            selected.append(target)
        return selected, {
            "source": "trace", "transport": "TRACE MCP staged export",
            "retrieved_at": retrieval["retrieved_at"], "profile": expected,
            "files": files, "fallback_reason": "AQUA pull failed after three attempts"}
    except (OSError, KeyError, TypeError, ValueError, RecursionError) as error:
        raise SharedDashboardError(f"Staged TRACE MCP export is invalid: {error}", 422) from error


def _stage_connector_files(fetcher, fields, fetch_folder, selected_folder):
    from large_data_store import SUPPORTED_SUFFIXES

    fetched = fetcher(list(fields["lots"]), fields["test_program"], fields["product"], fetch_folder)
    if not isinstance(fetched, list) or not fetched:
        raise ValueError("AQUA returned no files")
    seen = set()
    selected_folder.mkdir(parents=True, exist_ok=True)
    for index, item in enumerate(fetched):
        if not isinstance(item, (str, Path)):
            raise ValueError("Invalid file path")
        returned = _safe_path(fetch_folder, item)
        if (not returned.is_file() or returned.suffix.lower() not in SUPPORTED_SUFFIXES
                or returned.name.startswith("~$") or returned in seen):
            raise ValueError("Invalid supported file")
        seen.add(returned)
        shutil.copy2(returned, selected_folder / f"{index}-{returned.name}")
    retrieval = None
    sidecar = fetch_folder / "retrieval.json"
    if sidecar.is_file():
        retrieval = _read_json(sidecar)
    parameter_copy = None
    if retrieval and retrieval.get("parameters"):
        from aqua_connector import PARAMETER_FILES

        parameter_copy = (PARAMETER_FILES[fields["data_domain"]],
                          (fetch_folder / PARAMETER_FILES[fields["data_domain"]]).read_bytes())
    return retrieval, parameter_copy


def _body():
    if request.mimetype != "application/json":
        raise SharedDashboardError("Content-Type must be application/json.", 415)
    try:
        body = _decode(request.get_data().decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise SharedDashboardError("Malformed JSON body (duplicate keys and nonfinite numbers are rejected).") from error
    if not isinstance(body, dict):
        raise SharedDashboardError("JSON body must be an object.")
    return body


def create_app(root: Path, publish_folder: Path, service_url=None):
    """Create the JSON/HTML service without opening a cache or contacting a network."""
    root, publish_folder = Path(root).resolve(), Path(publish_folder).resolve()
    service_url = _service_url(service_url)
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY
    jobs = {}
    job_lock = threading.Lock()

    def start_job(slug, pull=True, data_domain=None):
        _load_profile(root, slug)
        with job_lock:
            if slug in jobs and jobs[slug]['status'] in ('queued', 'running'):
                return dict(jobs[slug])
            job = {'id': uuid.uuid4().hex, 'slug': slug, 'status': 'queued',
                   'created_at': _now(), 'message': 'Queued for selected categories.'}
            path = _safe_path(_state_folder(root, slug), 'job.json')
            jobs[slug] = job
            _atomic_json(path, job)

            def run():
                try:
                    with _file_lock(_safe_path(_state_folder(root, slug), '.processing.lock')):
                        job.update(status='running', message='Starting selected categories.')
                        _atomic_json(path, job)
                        started = time.monotonic()

                        def progress(message):
                            job.update(message=message, elapsed_seconds=round(time.monotonic() - started))
                            _atomic_json(path, job)

                        result = _process(root, publish_folder, slug, pull, data_domain, progress=progress)
                        result = _publish_completed(root, publish_folder, service_url, result)
                        counts = result['category_counts']
                        enabled = result.get('snapshot', {}).get('profile', {}).get('analysis_categories', CATEGORIES)
                        empty = [CATEGORY_TITLES[key] for key in enabled if not counts[key]['readings']]
                        message = 'Processing complete.'
                        if empty:
                            message += ' No matching data: ' + ', '.join(empty) + '.'
                        if result.get('warning'):
                            message += ' ' + result['warning']
                        job.update(status='completed', message=message, category_counts=counts,
                                   version=result['snapshot']['version'])
                except Exception as error:
                    app.logger.exception('Background report processing failed')
                    job.update(status='failed', message=str(error))
                finally:
                    job['finished_at'] = _now()
                    _atomic_json(path, job)

            worker = threading.Thread(target=run, name='thermal-' + job['id'], daemon=True)
            try:
                worker.start()
            except Exception:
                job.update(status='failed', message='Could not start the processing worker.', finished_at=_now())
                _atomic_json(path, job)
                raise
            return dict(job)
    # Also block DNS-rebinding Host headers on the default loopback service.
    app.config["TRUSTED_HOSTS"] = ["localhost", "127.0.0.1", "[::1]"]
    if service_url:
        app.config["TRUSTED_HOSTS"].append(urlsplit(service_url).hostname)

    @app.before_request
    def protect_mutations():
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            baseline = app.config.get('BASELINE_SLUG')
            if baseline and request.path in (f'/api/profiles/{baseline}', f'/api/profiles/{baseline}/process',
                                             f'/api/profiles/{baseline}/jobs'):
                raise SharedDashboardError('The release baseline is read-only. Copy the report to create your own analysis.', 403)
            site = request.headers.get("Sec-Fetch-Site", "").lower()
            if site not in ("", "none", "same-origin"):
                raise SharedDashboardError("Cross-origin mutations are not allowed.", 403)
            origin = request.headers.get("Origin")
            if origin is not None and (_origin(origin) is None or _origin(origin) != _origin(request.host_url)):
                raise SharedDashboardError("Origin must match the service origin.", 403)

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.path == "/" or request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(SharedDashboardError)
    def expected_error(error):
        return jsonify(error=str(error)), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @app.get("/")
    def portal():
        data = _library(root, service_url, requested_category="thermal_diode")
        if app.config.get('BASELINE_SLUG'):
            data['baseline_slug'] = app.config['BASELINE_SLUG']
            data['baseline_readonly'] = True
        return Response(_render(root, data), mimetype="text/html")

    @app.get('/api/workspace-info')
    def workspace_info():
        return jsonify(application='Thermal Analysis Dashboard', workspace=str(root))

    @app.get("/api/profiles")
    def profiles_list():
        category = request.args.get("category")
        if category is not None and category not in CATEGORIES:
            raise SharedDashboardError("Invalid analysis category.")
        data = _library(root, service_url, requested_category=category)
        if app.config.get('BASELINE_SLUG'):
            data['baseline_slug'] = app.config['BASELINE_SLUG']
            data['baseline_readonly'] = True
        return jsonify(data)

    @app.post("/api/profiles")
    def profiles_create():
        fields = _profile_fields(_body())
        with _locked(root, publish_folder):
            _validate_product(root, fields)
            identifier = uuid.uuid4()
            prefix = re.sub(r"[^A-Za-z0-9_-]+", "-", fields["work_week"]).strip("-_")[:40] or "profile"
            slug = f"{prefix}-{identifier.hex}"
            path = _profile_path(root, slug)
            if path.exists():
                raise SharedDashboardError("Report ID collision; retry creation.", 409)
            now = _now()
            profile = dict(fields, slug=slug, id=str(identifier), created=now, updated=now)
            if _safe_path(root, 'config', 'owner_storage.json').is_file():
                profile['owner_storage'] = _save_owner_report(root, profile)
            _atomic_json(path, profile)
            result = _auto_publish(root, publish_folder, service_url, _metadata(root, slug), 'Report created')
        if fields['source'] == 'aqua' and app.config.get('AUTO_PROCESS_NEW_REPORTS', True):
            result['job'] = start_job(slug, True, fields['data_domain'])
        response = jsonify(result)
        response.status_code = 201
        response.headers['Location'] = f'/api/profiles/{slug}'
        return response

    @app.post('/api/owners')
    def owners_create():
        body = _body()
        if set(body) != {'name'} or not isinstance(body['name'], str):
            raise SharedDashboardError('Provide an owner name.')
        name = body['name'].strip()
        folder = _register_owner(root, name)
        return jsonify(name=name, folder=str(folder)), 201

    @app.route('/api/profiles/<slug>/jobs', methods=['GET', 'POST'])
    def profiles_jobs(slug):
        slug = _slug(slug)
        _load_profile(root, slug)
        if request.method == 'POST':
            body = _body()
            if (set(body) - {'pull', 'data_domain'} or not isinstance(body.get('pull', True), bool)
                    or ('data_domain' in body and body['data_domain'] not in ('CLASS', 'SORT'))):
                raise SharedDashboardError('Jobs accept boolean pull and data_domain CLASS or SORT.')
            return jsonify(start_job(slug, body.get('pull', True), body.get('data_domain'))), 202
        path = _safe_path(_state_folder(root, slug), 'job.json')
        job = _read_json(path) if path.is_file() else None
        if job and job['status'] in ('queued', 'running') and slug not in jobs:
            job.update(status='interrupted', message='Server restarted before completion. Last saved snapshot retained.')
        return jsonify(job=job)

    @app.get("/api/profiles/<slug>")
    def profiles_detail(slug):
        return jsonify(_metadata(root, _slug(slug)))

    @app.put("/api/profiles/<slug>")
    def profiles_update(slug):
        slug = _slug(slug)
        fields = _profile_fields(_body())
        with _file_lock(_safe_path(_state_folder(root, slug), '.processing.lock')), _locked(root, publish_folder):
            profile = _load_profile(root, slug)
            if profile.get('owner_storage') and fields['owner'] != profile['owner']:
                raise SharedDashboardError('Copy the report to change its owner.')
            _validate_product(root, fields)
            previous = copy.deepcopy(profile)
            profile.update(fields, updated=_now())
            if profile.get('owner_storage'):
                profile['owner_storage'] = _save_owner_report(root, profile)
            _atomic_json(_profile_path(root, slug), profile)
            try:
                report_updated = _refresh_product_report(root, publish_folder, slug, previous, profile)
            except Exception:
                _atomic_json(_profile_path(root, slug), previous)
                raise
            result = _metadata(root, slug)
            if fields['product'] in _product_names(root)[fields['data_domain']]:
                result['report_updated'] = report_updated
                if result['snapshot'] and result['snapshot'].get('report_product') != fields['product']:
                        result['warning'] = 'Report saved. Process the selected source data to update the product in its plots.'
            return jsonify(_auto_publish(root, publish_folder, service_url,
                                 result, "Report updated"))

    @app.delete("/api/profiles/<slug>")
    def profiles_delete(slug):
        slug = _slug(slug)
        if request.get_data() and _body():
            raise SharedDashboardError("Deleting accepts only an empty JSON object.")
        with _file_lock(_safe_path(_state_folder(root, slug), '.processing.lock')), _locked(root, publish_folder):
            _load_profile(root, slug)
            archive = _safe_path(root, "Profiles", "archive", f"{slug}-{uuid.uuid4().hex}.json")
            archive.parent.mkdir(parents=True, exist_ok=True)
            _profile_path(root, slug).rename(archive)
            return jsonify(_auto_publish(root, publish_folder, service_url,
                                         {"deleted": slug}, "Report deleted"))

    @app.post("/api/publish")
    def profiles_publish():
        if request.get_data() and _body():
            raise SharedDashboardError("Publishing accepts only an empty JSON object.")
        try:
            publish_portal(root, publish_folder, service_url)
        except SharedDashboardError:
            raise
        except Exception as error:
            app.logger.exception("Static index publication failed")
            raise SharedDashboardError("Static index publication failed; committed snapshots are unchanged.", 500) from error
        return jsonify(published=True)

    @app.post("/api/profiles/<slug>/process")
    def profiles_process(slug):
        slug = _slug(slug)
        body = _body()
        if (set(body) - {"pull", "data_domain", "category"} or not isinstance(body.get("pull", False), bool)
                or ('data_domain' in body and body['data_domain'] not in ('CLASS', 'SORT'))
                or ('category' in body and body['category'] not in CATEGORIES)):
            raise SharedDashboardError("Processing accepts boolean pull, data_domain CLASS or SORT, and a valid category.")
        try:
            with _file_lock(_safe_path(_state_folder(root, slug), '.processing.lock')):
                result = _process(root, publish_folder, slug, body.get("pull", False), body.get('data_domain'),
                                  body.get('category'))
                return jsonify(_publish_completed(root, publish_folder, service_url, result))
        except SharedDashboardError:
            raise
        except ValueError as error:
            raise SharedDashboardError(f"Processing failed: {error}", 422) from error
        except Exception as error:
            app.logger.exception("Report processing failed")
            raise SharedDashboardError("Processing failed; check the report for its latest committed snapshot.", 500) from error

    @app.post("/api/profiles/<slug>/powerpoint")
    def profiles_powerpoint(slug):
        slug = _slug(slug)
        body = _body()
        if (set(body) != {"category", "version", "data_domain"} or
                not isinstance(body.get("category"), str) or body["category"] not in CATEGORIES or
                not isinstance(body.get("version"), str) or not VERSION_PATTERN.fullmatch(body["version"]) or
                not isinstance(body.get("data_domain"), str) or body["data_domain"] not in ("CLASS", "SORT")):
            raise SharedDashboardError(
                "PowerPoint requires exactly category (thermal_diode, dts, dtsmon), "
                "version (32 lowercase hex characters), and data_domain (CLASS or SORT).")
        try:
            with _locked(root, publish_folder):
                result = _generate_powerpoint(root, slug, body)
                return jsonify(_auto_publish(root, publish_folder, service_url, result, "PowerPoint cached"))
        except SharedDashboardError:
            raise
        except Exception as error:
            app.logger.exception("PowerPoint generation failed")
            raise SharedDashboardError(
                "PowerPoint generation failed; committed HTML and snapshot metadata are unchanged.", 500) from error

    @app.get("/exports/<path:filename>")
    def powerpoints(filename):
        parts = filename.split("/")
        if len(parts) != 3 or not parts[2].endswith(".pptx"):
            raise SharedDashboardError("PowerPoint not found.", 404)
        slug, version, name = parts
        try:
            _powerpoint_relative(slug, version, name[:-5])
            _load_profile(root, slug)
            target = _powerpoint_path(root, slug, version, name[:-5])
        except SharedDashboardError as error:
            raise SharedDashboardError("PowerPoint not found.", 404) from error
        # Serve the local cache so publication failures do not block downloads.
        if not target.is_file():
            raise SharedDashboardError('PowerPoint not found.', 404)
        return send_file(target, as_attachment=True, download_name=name, mimetype=POWERPOINT_MIME)

    @app.get("/reports/<path:filename>")
    def reports(filename):
        if "\\" in filename or ":" in filename or any(part in (".", "..") for part in filename.split("/")):
            raise SharedDashboardError("Unsafe report path.", 404)
        reports_root = _safe_path(publish_folder, "reports")
        target = _safe_path(reports_root, filename)
        if any(part.startswith(".") for part in target.relative_to(reports_root).parts):
            raise SharedDashboardError("Report not found.", 404)
        if not target.is_file():
            raise SharedDashboardError('Report not found.', 404)
        return send_file(target)

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--publish-folder", type=Path, required=True)
    parser.add_argument("--service-url")
    parser.add_argument("--publish", action="store_true", help="Publish static index and exit")
    parser.add_argument("--port", type=int, default=8051)
    args = parser.parse_args(argv)
    if args.publish:
        print(publish_portal(args.root, args.publish_folder, args.service_url))
    else:
        create_app(args.root, args.publish_folder, args.service_url).run(
            host="127.0.0.1", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()