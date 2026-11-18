"""Disk-backed thermal readings with bounded ingestion and query results."""

import csv
import hashlib
import itertools
import json
import math
import threading
from pathlib import Path

import duckdb
import pandas as pd
from openpyxl import load_workbook


COLUMNS = [
    "Test", "Measurement_Point", "Value", "Unit_ID", "Lot", "DevRevStep",
    "Functional_Bin", "Thermal_Head_Id", "Unit_Tester_Id", "Unit_Tester_Site_Id",
    "Unit_TIU", "TIU_Personality_Card_ID", "Program_Name", "Product_Name",
    "BOM", "PKG", "Socket",
]
FILTER_COLUMNS = set(COLUMNS) - {"Value"}
SUPPORTED_SUFFIXES = {".csv", ".txt", ".xlsx"}
CACHE_VERSION = 10
RECIPE_FIELDS = {'CLASS': 'PCS_STIM_RECIPE_VERSION', 'SORT': 'RECIPEMANIFEST'}
RECIPE_COLUMNS = ['source', 'Unit_ID', 'Lot', 'Program_Name', 'Product_Name', 'Recipe_Field', 'Recipe_Revision', 'Socket']


def _text(value):
    return "" if value is None else str(value).encode("utf-8", errors="replace").decode("utf-8")


def _normalize_header(value):
    name = _text(value).strip()
    aliases = {"test_name": "TEST_NAME", "test_result": "TEST_RESULT",
               "test_result_numeric": "TEST_RESULT_NUMERIC", "visual_id": "VISUAL_ID",
               "lot": "Lot", "lots": "LOTS", "devrevstep": "DevRevStep", "bom": "BOM",
               "pkg": "PKG", "package": "PKG", "product": "Product_Name", "product_name": "Product_Name",
               "programname": "Program_Name", "socket": "Socket", "socket_name": "Socket",
               "socket name": "Socket", "processstep": "ProcessStep", "process_step": "ProcessStep",
               "process step": "ProcessStep", "subflowstep": "SubFlowStep", "subflow_step": "SubFlowStep",
               "subflow step": "SubFlowStep", "sub flow step": "SubFlowStep"}
    compact = ''.join(character for character in name.casefold() if character.isalnum())
    metadata_aliases = {
        'socket': 'Socket', 'socketname': 'Socket', 'socketid': 'Socket',
        'processstep': 'ProcessStep', 'subflowstep': 'SubFlowStep',
        'opergroup': 'OPERGROUP',
        'thermalhead': 'Thermal Head Id', 'thermalheadid': 'Thermal Head Id',
        'unittesterid': 'Unit Tester Id', 'testerid': 'Unit Tester Id', 'tester': 'Unit Tester Id',
        'unittestersiteid': 'Unit Tester Site Id', 'testersiteid': 'Unit Tester Site Id',
    }
    return metadata_aliases.get(compact, aliases.get(name.lower(), name))


def _socket_value(row):
    for key in ('Socket', 'ProcessStep', 'SubFlowStep', 'OPERGROUP'):
        value = _text(row.get(key)).strip()
        if key == 'OPERGROUP':
            operation, separator, value = value.partition('_')
            if not separator or not operation.isdecimal():
                continue
        if value.casefold() in ('', 'n/a', 'na', 'nan', 'none', 'null', '-', 'unknown', 'unknown socket'):
            continue
        compact = ''.join(character for character in value.upper() if character.isalnum())
        if compact in {'CLASSHOT', 'CLASSCOLD', 'CLASSROOM', 'CSM', 'PHMHOT', 'PHMCOLD', 'PHMROOM'}:
            return compact
        return value
    return ''


def _reference_sensor_results(result, sensors):
    result = result.strip()
    if result in ('', 'nan', 'None'):
        return {}
    parsed = {}
    if ':' in result and '=' not in result:
        for token in result.split('|'):
            sensor, separator, value = token.partition(':')
            sensor = sensor.strip()
            if separator and sensor and not any(character in sensor for character in ',;=\r\n'):
                parsed[sensor] = value.strip()
    elif '|' in result:
        parsed.update(zip(sensors, (value.strip() for value in result.split('|'))))
    elif ',' in result and not any(character in result for character in '=:'):
        parsed.update(zip(sensors, (value.strip() for value in result.split(','))))
    elif '=' in result:
        for token in result.split(';'):
            sensor, separator, value = token.partition('=')
            if separator:
                parsed[sensor.strip()] = value.strip()
    elif sensors:
        parsed[sensors[0]] = result
    return parsed


def _source_rows(path):
    if path.suffix.lower() == ".xlsx":
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            rows = workbook.worksheets[0].iter_rows(values_only=True)
            header = [_normalize_header(value) for value in next(rows, ())]
            if "TEST_NAME" not in header or not {"TEST_RESULT", "TEST_RESULT_NUMERIC"}.intersection(header):
                raise ValueError(f"{path.name}: TEST_NAME and TEST_RESULT or TEST_RESULT_NUMERIC are required")
            for values in rows:
                yield dict(zip(header, values))
        finally:
            workbook.close()
    else:
        encoding = "utf-8-sig"
        try:
            with path.open(encoding=encoding) as stream:
                for _block in iter(lambda: stream.read(1024 * 1024), ""):
                    pass
        except UnicodeDecodeError:
            encoding = "latin-1"
        with path.open(encoding=encoding, newline="") as stream:
            reader = csv.DictReader(stream, delimiter="\t" if path.suffix.lower() == ".txt" else ",")
            reader.fieldnames = [_normalize_header(value) for value in (reader.fieldnames or [])]
            if "TEST_NAME" not in reader.fieldnames or not {"TEST_RESULT", "TEST_RESULT_NUMERIC"}.intersection(reader.fieldnames):
                raise ValueError(f"{path.name}: TEST_NAME and TEST_RESULT or TEST_RESULT_NUMERIC are required")
            yield from reader


class ThermalStore:
    def __init__(self, database, memory_limit="1GB", batch_size=5000, threads=1):
        if batch_size < 1 or threads < 1:
            raise ValueError("batch_size and threads must be positive")
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.batch_size = batch_size
        self.lock = threading.RLock()
        self.connection = duckdb.connect(str(self.database))
        try:
            self.connection.execute("SET memory_limit = ?", [memory_limit])
            self.connection.execute("SET threads = ?", [threads])
            self.connection.execute("SET preserve_insertion_order = false")
            self.connection.execute("SET temp_directory = ?", [str(self.database.with_suffix(".spill"))])
            schema = ", ".join(f'"{column}" {"DOUBLE" if column == "Value" else "VARCHAR"}' for column in COLUMNS)
            self.connection.execute(f"CREATE TABLE IF NOT EXISTS readings (source VARCHAR, {schema})")
            for column in ("BOM", "PKG", "Socket"):
                self.connection.execute(f'ALTER TABLE readings ADD COLUMN IF NOT EXISTS "{column}" VARCHAR DEFAULT \'\'')
            self.connection.execute("CREATE TABLE IF NOT EXISTS sources (path VARCHAR PRIMARY KEY, fingerprint VARCHAR, skipped BIGINT)")
            recipe_schema = ', '.join(f'"{column}" VARCHAR' for column in RECIPE_COLUMNS)
            self.connection.execute(f'CREATE TABLE IF NOT EXISTS recipe_revisions ({recipe_schema})')
            self.connection.execute("ALTER TABLE recipe_revisions ADD COLUMN IF NOT EXISTS Socket VARCHAR DEFAULT ''")
        except Exception:
            self.connection.close()
            raise

    def close(self):
        with self.lock:
            self.connection.close()

    def invalidate(self):
        with self.lock:
            self.connection.execute("UPDATE sources SET fingerprint = ''")

    def is_current(self, folder, config, product_mapping):
        settings = json.dumps([CACHE_VERSION, config, product_mapping], sort_keys=True)
        settings_hash = hashlib.sha256(settings.encode()).hexdigest()
        current = {}
        for path in Path(folder).iterdir():
            if path.is_file() and not path.name.startswith('~$') and path.suffix.lower() in SUPPORTED_SUFFIXES:
                stat = path.stat()
                current[str(path.resolve())] = f'{stat.st_size}:{stat.st_mtime_ns}:{settings_hash}'
        with self.lock:
            known = dict(self.connection.execute('SELECT path, fingerprint FROM sources').fetchall())
        return bool(current) and known == current

    def _insert(self, batch):
        frame = pd.DataFrame.from_records(batch, columns=["source"] + COLUMNS)
        self.connection.register("ingest_batch", frame)
        try:
            columns = ", ".join(f'"{column}"' for column in ["source"] + COLUMNS)
            self.connection.execute(f"INSERT INTO readings ({columns}) SELECT {columns} FROM ingest_batch")
        finally:
            self.connection.unregister("ingest_batch")

    def _insert_recipes(self, batch):
        frame = pd.DataFrame.from_records(batch, columns=RECIPE_COLUMNS)
        self.connection.register('recipe_batch', frame)
        try:
            self.connection.execute('INSERT INTO recipe_revisions SELECT DISTINCT * FROM recipe_batch')
        finally:
            self.connection.unregister('recipe_batch')

    def refresh(self, folder, config, product_mapping, progress=None):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        files = sorted(path for path in folder.iterdir() if path.is_file() and not path.name.startswith("~$"))
        unsupported = [path.name for path in files if path.suffix.lower() == ".xls"]
        if unsupported:
            raise ValueError("Convert legacy .xls files to .xlsx or CSV: " + ", ".join(unsupported))
        files = [path for path in files if path.suffix.lower() in SUPPORTED_SUFFIXES]
        settings = json.dumps([CACHE_VERSION, config, product_mapping], sort_keys=True)
        settings_hash = hashlib.sha256(settings.encode()).hexdigest()
        prefixes = sorted(product_mapping, key=len, reverse=True)
        with self.lock:
            known = dict(self.connection.execute("SELECT path, fingerprint FROM sources").fetchall())
            current = {str(path.resolve()) for path in files}
            for source in known.keys() - current:
                self.connection.execute("BEGIN TRANSACTION")
                try:
                    self.connection.execute("DELETE FROM readings WHERE source = ?", [source])
                    self.connection.execute("DELETE FROM recipe_revisions WHERE source = ?", [source])
                    self.connection.execute("DELETE FROM sources WHERE path = ?", [source])
                    self.connection.execute("COMMIT")
                except Exception:
                    try:
                        self.connection.execute("ROLLBACK")
                    except duckdb.TransactionException:
                        pass
                    raise
            for path in files:
                source = str(path.resolve())
                stat = path.stat()
                fingerprint = f"{stat.st_size}:{stat.st_mtime_ns}:{settings_hash}"
                if known.get(source) == fingerprint:
                    continue
                if progress:
                    progress(f"Importing {path.name}")

                rows = iter(_source_rows(path))
                first_row = next(rows, None)
                rows = itertools.chain((first_row,) if first_row is not None else (), rows)

                # Replace this source in bounded transactions. Staging a full
                # expanded copy can require more temporary disk than the source
                # itself, so ingest each batch directly into DuckDB instead.
                self.connection.execute("BEGIN TRANSACTION")
                try:
                    self.connection.execute("DELETE FROM readings WHERE source = ?", [source])
                    self.connection.execute("DELETE FROM recipe_revisions WHERE source = ?", [source])
                    self.connection.execute("DELETE FROM sources WHERE path = ?", [source])
                    self.connection.execute("COMMIT")
                except Exception:
                    try:
                        self.connection.execute("ROLLBACK")
                    except duckdb.TransactionException:
                        pass
                    raise
                batch = []
                recipe_batch = []
                skipped = 0
                try:
                    for row in rows:
                        test = _text(row.get("TEST_NAME"))
                        sensors = config.get(test, {}).get("COLUMN_NAMES", [])
                        if isinstance(sensors, str):
                            sensors = [name.strip() for name in sensors.split(",")]
                        sensors = sensors if isinstance(sensors, list) else []
                        devrev = _text(row.get("DevRevStep"))
                        product = _text(row.get("Product_Name")) or next(
                            (product_mapping[prefix] for prefix in prefixes if devrev.startswith(prefix)), f"Unknown_Product_{devrev}")
                        socket = _socket_value(row)
                        metadata = [
                            _text(row.get("VISUAL_ID")), _text(row.get("Lot", row.get("LOTS"))), devrev,
                            _text(row.get("FUNCTIONAL_BIN")), _text(row.get("Thermal Head Id")),
                            _text(row.get("Unit Tester Id")), _text(row.get("Unit Tester Site Id")),
                            _text(row.get("Unit TIU")), _text(row.get("TIU Personality Card ID")),
                            _text(row.get("Program Name", row.get("Program_Name"))), product,
                            _text(row.get("BOM")), _text(row.get("PKG")), socket,
                        ]
                        result = _text(row.get("TEST_RESULT"))
                        # AQUA scalar tests often leave the string result blank.
                        if not result.strip():
                            result = _text(row.get("TEST_RESULT_NUMERIC"))
                        for field in RECIPE_FIELDS.values():
                            revision = result if test == field else _text(row.get(field))
                            if revision.strip():
                                recipe_batch.append([source, metadata[0], metadata[1], metadata[9], product,
                                                     field, revision, socket])
                                if len(recipe_batch) >= self.batch_size:
                                    self._insert_recipes(recipe_batch)
                                    recipe_batch = []
                        if test in RECIPE_FIELDS.values():
                            continue
                        if config.get(test, {}).get('RESULT_PARSER') == 'dts_reference':
                            if row.get('PARAMETER_GROUP') == 'PARAMETRIC' and 'TEST_RESULT_NUMERIC' in row:
                                result = _text(row['TEST_RESULT_NUMERIC'])
                            for sensor, raw_value in _reference_sensor_results(result, sensors).items():
                                try:
                                    value = float(raw_value)
                                except ValueError:
                                    skipped += 1
                                    continue
                                if not math.isfinite(value):
                                    skipped += 1
                                    continue
                                batch.append([source, test, sensor, value] + metadata)
                                if len(batch) >= self.batch_size:
                                    self._insert(batch)
                                    batch = []
                            continue
                        for index, token in enumerate(result.split("|")):
                            sensor, separator, raw_value = token.partition(":")
                            if separator:
                                sensor = sensor.strip()
                                if not sensor or any(char in sensor for char in ",;=\r\n"):
                                    skipped += 1
                                    continue
                            else:
                                # Positional values retain their original token index,
                                # even when other tokens are named or invalid.
                                sensor = _text(sensors[index]) if index < len(sensors) else f"Sensor_{index}"
                                raw_value = token
                            try:
                                value = float(raw_value.strip())
                            except ValueError:
                                skipped += 1
                                continue
                            if not math.isfinite(value):
                                skipped += 1
                                continue
                            batch.append([source, test, sensor, value] + metadata)
                            if len(batch) >= self.batch_size:
                                self._insert(batch)
                                batch = []
                    if batch:
                        self._insert(batch)
                    if recipe_batch:
                        self._insert_recipes(recipe_batch)
                    final_stat = path.stat()
                    if (stat.st_size, stat.st_mtime_ns) != (final_stat.st_size, final_stat.st_mtime_ns):
                        raise RuntimeError(f"{path.name} changed during import; retry after the writer finishes")

                    # Mark the source current only after every batch succeeded.
                    self.connection.execute("BEGIN TRANSACTION")
                    try:
                        self.connection.execute("INSERT INTO sources VALUES (?, ?, ?)",
                                                [source, fingerprint, skipped])
                        self.connection.execute("COMMIT")
                    except Exception:
                        try:
                            self.connection.execute("ROLLBACK")
                        except duckdb.TransactionException:
                            pass
                        raise
                except Exception:
                    # Remove partial batches; a retry will then start cleanly.
                    self.connection.execute("BEGIN TRANSACTION")
                    try:
                        self.connection.execute("DELETE FROM readings WHERE source = ?", [source])
                        self.connection.execute("DELETE FROM recipe_revisions WHERE source = ?", [source])
                        self.connection.execute("DELETE FROM sources WHERE path = ?", [source])
                        self.connection.execute("COMMIT")
                    except Exception:
                        try:
                            self.connection.execute("ROLLBACK")
                        except duckdb.TransactionException:
                            pass
                        raise
                    raise
            return self.connection.execute("SELECT count(*), coalesce(sum(skipped), 0) FROM sources").fetchone()

    def _where(self, filters):
        clauses = []
        parameters = []
        for column, value in (filters or {}).items():
            if column not in FILTER_COLUMNS:
                raise ValueError(f"Unsupported filter: {column}")
            if value is not None:
                if isinstance(value, (list, tuple)):
                    if not value:
                        continue
                    clauses.append(f'"{column}" IN ({", ".join("?" for _ in value)})')
                    parameters.extend(value)
                else:
                    clauses.append(f'"{column}" = ?')
                    parameters.append(value)
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), parameters

    def summary(self, filters=None):
        where, parameters = self._where(filters)
        with self.lock:
            return self.connection.execute(
                "SELECT count(*) AS readings, avg(Value) AS mean, stddev_pop(Value) AS stddev, "
                "min(Value) AS minimum, max(Value) AS maximum FROM readings" + where, parameters
            ).fetchdf().to_dict("records")[0]

    def overview(self, filters=None):
        where, parameters = self._where(filters)
        with self.lock:
            return self.connection.execute(
                'SELECT count(*) AS readings, count(DISTINCT Product_Name) AS products, '
                'count(DISTINCT Test) AS tests, count(DISTINCT Unit_ID) AS units, '
                'count(DISTINCT Lot) AS lots FROM readings' + where, parameters
            ).fetchdf().to_dict('records')[0]

    def values(self, column, filters=None):
        if column not in FILTER_COLUMNS:
            raise ValueError(f"Unsupported option: {column}")
        where, parameters = self._where(filters)
        with self.lock:
            return [row[0] for row in self.connection.execute(
                f'SELECT DISTINCT "{column}" FROM readings{where} ORDER BY 1', parameters
            ).fetchall()]

    def plot_rows(self, filters=None, limit=5000):
        if not 1 <= limit <= 20000:
            raise ValueError("Plot point limit must be between 1 and 20000")
        where, parameters = self._where(filters)
        with self.lock:
            return self.connection.execute(
                f'SELECT * EXCLUDE (source) FROM (SELECT * FROM readings{where}) '
                f'USING SAMPLE reservoir({int(limit)} ROWS) REPEATABLE(42)', parameters
            ).fetchdf()

    def test_stats(self, filters=None):
        where, parameters = self._where(filters)
        with self.lock:
            return self.connection.execute(
                'SELECT Test, count(*) AS count, avg(Value) AS mean, '
                'coalesce(stddev_samp(Value), 0) AS std FROM readings' + where +
                ' GROUP BY Test ORDER BY Test', parameters
            ).fetchdf().set_index('Test')

    def boxes(self, filters=None):
        where, parameters = self._where(filters)
        with self.lock:
            return self.connection.execute(
                'WITH selected AS (SELECT Product_Name, Measurement_Point, Value FROM readings' + where + '), '
                'quartiles AS (SELECT Product_Name, Measurement_Point, '
                'quantile_cont(Value, 0.25) AS q1, median(Value) AS median, '
                'quantile_cont(Value, 0.75) AS q3, avg(Value) AS mean, stddev_pop(Value) AS sd '
                'FROM selected GROUP BY Product_Name, Measurement_Point) '
                'SELECT quartiles.*, min(Value) FILTER (WHERE Value >= q1 - 1.5 * (q3 - q1)) AS minimum, '
                'max(Value) FILTER (WHERE Value <= q3 + 1.5 * (q3 - q1)) AS maximum '
                'FROM quartiles JOIN selected USING (Product_Name, Measurement_Point) '
                'GROUP BY ALL ORDER BY Product_Name, Measurement_Point', parameters
            ).fetchdf()

    def commonality(self, filters, bin_value, parameter):
        if parameter not in FILTER_COLUMNS:
            raise ValueError("Unsupported commonality parameter")
        where, parameters = self._where(filters)
        with self.lock:
            return self.connection.execute(
                f'WITH units AS (SELECT Unit_ID, first(Functional_Bin ORDER BY source, Test, Lot) AS bin, '
                f'first("{parameter}" ORDER BY source, Test, Lot) AS label FROM readings{where} GROUP BY Unit_ID) '
                'SELECT label, count(*) AS "Failed Units", sum(count(*)) OVER () AS total FROM units WHERE bin = ? '
                'GROUP BY label ORDER BY "Failed Units" DESC LIMIT 200', parameters + [str(bin_value)]
            ).fetchdf()

    def repeat_failures(self, filters, minimum=2):
        where, parameters = self._where(filters)
        where += (' AND ' if where else ' WHERE ') + "Functional_Bin NOT IN ('', '1') AND lower(Functional_Bin) != 'nan'"
        query = (
            'WITH events AS (SELECT DISTINCT Unit_ID, Test, Functional_Bin, Lot FROM readings' + where + '), '
            'grouped AS (SELECT Unit_ID, count(*) AS Failure_Count, count(DISTINCT Test) AS Unique_Tests, '
            "array_to_string(list(DISTINCT Functional_Bin ORDER BY Functional_Bin), ', ') AS Unique_Bins, "
            "array_to_string(list_slice(list(DISTINCT Test ORDER BY Test), 1, 5), ', ') AS Tests, "
            "array_to_string(list_slice(list(DISTINCT Lot ORDER BY Lot), 1, 3), ', ') AS Lot FROM events GROUP BY Unit_ID) "
        )
        with self.lock:
            total, repeated = self.connection.execute(
                query + 'SELECT count(*), count(*) FILTER (WHERE Failure_Count >= ?) FROM grouped', parameters + [minimum]
            ).fetchone()
            rows = self.connection.execute(
                query + 'SELECT * FROM grouped WHERE Failure_Count >= ? ORDER BY Failure_Count DESC, Unit_ID LIMIT 200',
                parameters + [minimum]
            ).fetchdf()
        return rows, total, repeated

    def options(self, column, filters=None, search="", selected=None):
        if column not in FILTER_COLUMNS:
            raise ValueError(f"Unsupported option: {column}")
        where, parameters = self._where({key: value for key, value in (filters or {}).items() if key != column})
        where += (" AND " if where else " WHERE ") + f'contains(lower("{column}"), lower(?))'
        with self.lock:
            values = [row[0] for row in self.connection.execute(
                f'SELECT DISTINCT "{column}" FROM readings{where} ORDER BY 1 LIMIT 200', parameters + [search or ""]
            ).fetchall()]
        selections = selected if isinstance(selected, (list, tuple)) else [selected]
        for value in selections:
            if value is not None and value not in values:
                values.insert(0, value)
        return [{"label": value or "(blank)", "value": value} for value in values]

    def sample(self, filters=None, limit=5000):
        if not 1 <= limit <= 20000:
            raise ValueError("Plot point limit must be between 1 and 20000")
        where, parameters = self._where(filters)
        with self.lock:
            return self.connection.execute(
                'SELECT Test, Measurement_Point, Value, Unit_ID, Lot FROM '
                f'(SELECT * FROM readings{where}) USING SAMPLE reservoir({int(limit)} ROWS) REPEATABLE(42)',
                parameters,
            ).fetchdf()

    def _statistics_query(self, filters, by_lot):
        where, parameters = self._where(filters)
        groups = ["Product_Name", "BOM", "PKG", "Test", "Measurement_Point"] + (["Lot"] if by_lot else [])
        grouping = ", ".join(f'"{column}"' for column in groups)
        query = (
            f'SELECT {grouping}, count(*) AS readings, '
            'avg(Value) AS mean, stddev_pop(Value) AS stddev, min(Value) AS minimum, max(Value) AS maximum '
            f'FROM readings{where} GROUP BY {grouping}'
        )
        return query, parameters, groups

    def iter_statistics(self, filters=None, batch_size=200, by_lot=False):
        if not 1 <= batch_size <= 20000:
            raise ValueError("Statistics batch size must be between 1 and 20000")
        query, parameters, groups = self._statistics_query(filters, by_lot)
        order = ", ".join(f'"{column}"' for column in groups)
        with self.lock:
            cursor = self.connection.cursor()
            try:
                cursor.execute(query + f' ORDER BY {order}', parameters)
                columns = [description[0] for description in cursor.description]
                while rows := cursor.fetchmany(batch_size):
                    yield pd.DataFrame.from_records(rows, columns=columns)
            finally:
                cursor.close()

    def statistics(self, filters=None, page=0, page_size=50, sort_by=None, by_lot=False):
        if page < 0 or not 1 <= page_size <= 200:
            raise ValueError("Invalid statistics page")
        query, parameters, groups = self._statistics_query(filters, by_lot)
        allowed = set(groups) | {"readings", "mean", "stddev", "minimum", "maximum"}
        order = []
        for item in sort_by or []:
            if item["column_id"] not in allowed or item["direction"] not in {"asc", "desc"}:
                raise ValueError("Invalid statistics sort")
            order.append(f'"{item["column_id"]}" {item["direction"]}')
        order.extend(f'"{column}"' for column in groups)
        with self.lock:
            count = self.connection.execute(f"SELECT count(*) FROM ({query})", parameters).fetchone()[0]
            rows = self.connection.execute(
                query + f' ORDER BY {", ".join(order)} LIMIT ? OFFSET ?', parameters + [page_size, page * page_size]
            ).fetchdf().to_dict("records")
        return rows, max(1, math.ceil(count / page_size))

    def export(self, destination, filters=None):
        destination = Path(destination)
        if destination.suffix.lower() not in {".csv", ".parquet"}:
            raise ValueError("Export must be CSV or Parquet")
        where, parameters = self._where(filters)
        columns = ", ".join(f'"{column}"' for column in COLUMNS)
        target = str(destination).replace("'", "''")
        options = "FORMAT PARQUET, COMPRESSION ZSTD" if destination.suffix.lower() == ".parquet" else "FORMAT CSV, HEADER TRUE"
        with self.lock:
            self.connection.execute(
                f"COPY (SELECT {columns} FROM readings{where}) TO '{target}' ({options})", parameters
            )