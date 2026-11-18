import copy
import csv
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from large_data_store import CACHE_VERSION, ThermalStore, _normalize_header


class NamedSensorDataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inputs = self.root / "input"
        self.inputs.mkdir()
        self.source = self.inputs / "thermal.csv"
        self.store = ThermalStore(self.root / "data.duckdb", batch_size=2)
        self.addCleanup(self.store.close)
        self.config = {"DTS": {"COLUMN_NAMES": ["Configured Z", "Configured A", "Configured M"]}}

    def write_rows(self, results, **metadata):
        with self.source.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["TEST_NAME", "TEST_RESULT", *metadata])
            for result in results:
                writer.writerow(["DTS", result, *metadata.values()])

    def refresh(self):
        return self.store.refresh(self.inputs, self.config, {"REV": "Mapped product"})

    def readings(self):
        return self.store.connection.execute(
            'SELECT Measurement_Point, Value FROM readings ORDER BY Value'
        ).fetchall()

    def test_mixed_tokens_keep_labels_positions_and_bounded_batches(self):
        self.write_rows([" Named Z : 1 | 2 |Named A:3|4", " 5 ", "Named Z:6"])
        original_config = copy.deepcopy(self.config)
        with patch.object(self.store, "_insert", wraps=self.store._insert) as insert:
            self.assertEqual(self.refresh(), (1, 0))
            self.assertEqual([len(call.args[0]) for call in insert.call_args_list], [2, 2, 2])
        self.assertEqual(self.readings(), [
            ("Named Z", 1), ("Configured A", 2), ("Named A", 3),
            ("Sensor_3", 4), ("Configured Z", 5), ("Named Z", 6),
        ])
        self.assertEqual(self.config, original_config)

    def test_named_tokens_never_invent_or_use_configured_names(self):
        self.config = {}
        self.write_rows([" Die Z : 1 |Die A:2|Die Z:3|Die M:4"])
        self.assertEqual(self.refresh(), (1, 0))
        self.assertEqual(self.readings(), [("Die Z", 1), ("Die A", 2), ("Die Z", 3), ("Die M", 4)])
        self.assertEqual(self.store.values("Measurement_Point"), ["Die A", "Die M", "Die Z"])
        self.assertEqual(self.config, {})

    def test_invalid_named_and_nonfinite_tokens_are_skipped_once(self):
        invalid = [
            "nan", "inf", "-inf", "1e999", "bad", "",
            "Die:nan", "Die:inf", "Die:-inf", "Die:1e999",
            "Die:bad", "Die:", ":5", "   :5", "Die:1:2",
            "Die,other:5", "Die;other:5", "Die=other:5",
            "Die\rother:5", "Die\nother:5",
        ]
        self.write_rows(["|".join([*invalid, " Valid die : -2.5e1 ", "26"])])
        self.assertEqual(self.refresh(), (1, len(invalid)))
        self.assertEqual(self.readings(), [("Valid die", -25), (f"Sensor_{len(invalid) + 1}", 26)])
        self.assertTrue(all(math.isfinite(value) for _, value in self.readings()))

    def test_scalar_and_positional_sensor_map_order(self):
        for names in (["Z", "A", "M"], " Z, A, M "):
            with self.subTest(names=names):
                self.config = {"DTS": {"COLUMN_NAMES": names}}
                self.write_rows(["10", "11|invalid:bad|13|14"])
                self.assertEqual(self.refresh(), (1, 1))
                self.assertEqual(self.readings(), [("Z", 10), ("Z", 11), ("M", 13), ("Sensor_3", 14)])

    def test_reference_delimiters_and_configured_positional_sensor_order(self):
        self.config['DTS']['RESULT_PARSER'] = 'dts_reference'
        self.config['DTS']['COLUMN_NAMES'] = ['AON', 'CCF0S0']
        self.write_rows(['10|20|999', '30,40,999', 'AON=50;CCF0S0=60',
                         'CCF0S0:70|AON:80', '90', 'AON:100|110|AON:120'])
        self.refresh()
        self.assertEqual(self.readings(), [
            ('AON', 10), ('CCF0S0', 20), ('AON', 30), ('CCF0S0', 40),
            ('AON', 50), ('CCF0S0', 60), ('CCF0S0', 70), ('AON', 80),
            ('AON', 90), ('AON', 120)])
        self.assertTrue(self.store.is_current(self.inputs, self.config, {'REV': 'Mapped product'}))
        self.config['DTS'].pop('RESULT_PARSER')
        self.assertFalse(self.store.is_current(self.inputs, self.config, {'REV': 'Mapped product'}))

    def test_reference_parametric_rows_use_numeric_result(self):
        self.config['DTS']['RESULT_PARSER'] = 'dts_reference'
        self.write_rows(['Unused:999'], PARAMETER_GROUP='PARAMETRIC', TEST_RESULT_NUMERIC='91')
        self.refresh()
        self.assertEqual(self.readings(), [('Configured Z', 91)])
        self.write_rows(['Unused:999'], PARAMETER_GROUP='PARAMETRIC', TEST_RESULT_NUMERIC='')
        self.refresh()
        self.assertEqual(self.readings(), [])

    def test_explicit_subflow_headers_preserve_socket_identity(self):
        for header in ('SubFlowStep', 'SUBFLOW_STEP', 'Subflow Step', 'Sub Flow Step'):
            with self.subTest(header=header):
                self.write_rows(['91'], **{header: 'CSM'})
                self.refresh()
                self.assertEqual(self.store.values('Socket'), ['CSM'])
        self.write_rows(['92'], Socket='CLASSHOT', SubFlowStep='CSM')
        self.refresh()
        self.assertEqual(self.store.values('Socket'), ['CLASSHOT'])
        self.write_rows(['93'], **{'Operation SubFlowStep Latest Flag': 'Y'})
        self.refresh()
        self.assertEqual(self.store.connection.execute('SELECT Socket FROM readings').fetchone()[0], '')

    def test_program_alias_and_metadata_are_preserved(self):
        metadata = {
            "VISUAL_ID": "0001", "LOTS": "0002", "DevRevStep": "REV1",
            "FUNCTIONAL_BIN": "03", "Thermal Head Id": "Head",
            "Unit Tester Id": "Tester", "Unit Tester Site Id": "04",
            "Unit TIU": "TIU", "TIU Personality Card ID": "Card",
            "ProgramName": "Program", "PRODUCT": "Explicit product",
            "BOM": "B01", "PKG": "P01",
        }
        expected = {
            "Unit_ID": "0001", "Lot": "0002", "DevRevStep": "REV1",
            "Functional_Bin": "03", "Thermal_Head_Id": "Head",
            "Unit_Tester_Id": "Tester", "Unit_Tester_Site_Id": "04",
            "Unit_TIU": "TIU", "TIU_Personality_Card_ID": "Card",
            "Program_Name": "Program", "Product_Name": "Explicit product",
            "BOM": "B01", "PKG": "P01",
        }
        for header in ("ProgramName", "Program_Name", "Program Name"):
            with self.subTest(header=header):
                row_metadata = dict(metadata)
                row_metadata[header] = row_metadata.pop("ProgramName")
                self.write_rows(["Named:1|2"], **row_metadata)
                self.assertEqual(self.refresh(), (1, 0))
                for row in self.store.plot_rows().to_dict("records"):
                    self.assertEqual({key: row[key] for key in expected}, expected)
        self.assertEqual(_normalize_header(" programname "), "Program_Name")
        for header in ("Custom Header", "Program_Name", "Program Name", "Thermal Head Id"):
            self.assertEqual(_normalize_header(header), header)
        self.write_rows(["Named:1"], DevRevStep="REV1")
        self.refresh()
        self.assertEqual(self.store.values("Product_Name"), ["Mapped product"])

    def test_old_cache_version_is_reimported_then_reused(self):
        self.assertGreater(CACHE_VERSION, 4)
        self.write_rows(["Named:1|nan"])
        with patch("large_data_store.CACHE_VERSION", 3):
            self.refresh()
        self.assertFalse(self.store.is_current(self.inputs, self.config, {"REV": "Mapped product"}))
        with patch.object(self.store, "_insert", wraps=self.store._insert) as insert:
            self.assertEqual(self.refresh(), (1, 1))
            insert.assert_called_once()
        self.assertTrue(self.store.is_current(self.inputs, self.config, {"REV": "Mapped product"}))
        with patch("large_data_store._source_rows", side_effect=AssertionError("Cache was not reused")):
            self.assertEqual(self.refresh(), (1, 1))
        self.assertEqual(self.readings(), [("Named", 1)])

    def test_numeric_result_fallback_only_for_blank_string_results(self):
        with self.source.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(['TEST_NAME', 'TEST_RESULT', 'TEST_RESULT_NUMERIC'])
            writer.writerows([['DTS', '', '21.8'], ['DTS', '   ', '0'],
                              ['DTS', 'CPU:30|31', '999'], ['DTS', 'bad', '40'],
                              ['DTS', '', 'nan'], ['DTS', '', 'inf']])
        self.assertEqual(self.refresh(), (1, 3))
        self.assertEqual(self.readings(), [('Configured Z', 0), ('Configured Z', 21.8),
                                          ('CPU', 30), ('Configured A', 31)])

    def test_numeric_only_csv_and_excel(self):
        from openpyxl import Workbook
        self.source.write_text('test_name,test_result_numeric\nDTS,79.4\n', encoding='utf-8')
        self.refresh()
        self.assertEqual(self.readings(), [('Configured Z', 79.4)])
        self.source.unlink()
        workbook = Workbook()
        workbook.active.append(['TEST_NAME', 'TEST_RESULT_NUMERIC'])
        workbook.active.append(['DTS', 77.5])
        workbook.save(self.inputs / 'numeric.xlsx')
        workbook.close()
        self.refresh()
        self.assertEqual(self.readings(), [('Configured Z', 77.5)])

    def test_aqua_validation_accepts_scalar_numeric_zero(self):
        from aqua_connector import _validate_download
        self.source.write_text('TEST_NAME,TEST_RESULT,TEST_RESULT_NUMERIC,Lot\nDTS,,0,L1\n', encoding='utf-8')
        result = _validate_download(self.source, 'L1', {'DTS'})
        self.assertEqual(result, {'operation': 'unverified', 'data_domain': 'unverified'})


if __name__ == "__main__":
    unittest.main()