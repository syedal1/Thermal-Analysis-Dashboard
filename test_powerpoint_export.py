"""Offline exporter tests: all chart rendering uses Pillow, never Chrome."""

import builtins
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import powerpoint_export as exporter


def plot_script(figure, attrs='class="plot-data" type="application/json"'):
    payload = json.dumps(figure).replace('<', '\\u003c').replace('>', '\\u003e')
    return f'<script {attrs}>{payload}</script>'


class ParserTests(unittest.TestCase):
    def test_summary_rows_stream_across_chunks_and_ignore_other_tables(self):
        html = ('<h2>Other table</h2><table><tr><td>ignored</td></tr></table>'
                '<h2>Full-data product / test statistics</h2><table><thead>'
                '<tr><th>Product_Name</th><th>Test</th><th>mean</th></tr></thead><tbody>'
                '<tr><td>HX28C</td><td>A &amp; B</td><td>100.18333333333331</td></tr>'
                '</tbody></table>')
        for size in (1, 7, 65536):
            found = []
            parser = exporter._PlotDataParser(self.fail, lambda headers, row: found.append((headers, row)))
            for start in range(0, len(html), size):
                parser.feed(html[start:start + size])
            parser.close()
            self.assertEqual(found, [(['Product_Name', 'Test', 'mean'],
                                     ['HX28C', 'A & B', '100.18333333333331'])])

    def test_invalid_summary_table_fails(self):
        for tail in ('<tr><td>1</td><td>2</td></tr></table>', '<tr><td>1</td></tr>'):
            with self.assertRaises(exporter.PowerPointExportError):
                parser = exporter._PlotDataParser(self.fail, lambda *args: None)
                parser.feed('<h2>Full-data product / test statistics</h2><table>'
                            '<tr><th>count</th></tr>' + tail)
                parser.close()

    def test_chunk_boundaries_attribute_orders_and_ignored_scripts(self):
        expected = [{'data': [], 'layout': {'title': {'text': 'A & B'}}}, {'data': []}]
        html = ('<script>window.not_json = true;</script>'
                '<script class="plot-data" type="text/javascript">ignored()</script>'
                '<script type="application/json">{"not":"a plot"}</script>'
                '<script src="https://invalid.example/chart.js" class="plot-data" '
                'type="application/json">ignored</script>'
                + plot_script(expected[0], 'class="extra plot-data more" type="application/json"')
                + plot_script(expected[1], 'type="application/json" class="plot-data extra"'))
        for size in (1, 7, 64, 65536):
            with self.subTest(size=size):
                found = []
                parser = exporter._PlotDataParser(lambda value: found.append(json.loads(value)))
                for start in range(0, len(html), size):
                    parser.feed(html[start:start + size])
                parser.close()
                self.assertEqual(found, expected)

    def test_large_ignored_javascript_is_not_buffered(self):
        parser = exporter._PlotDataParser(lambda value: self.fail('Unexpected chart'))
        parser.feed('<script>')
        for _ in range(100):
            parser.feed('x' * 65536)
            self.assertLess(len(parser.rawdata), 32)
        parser.feed('</script>')
        parser.close()

    def test_chart_is_delivered_before_later_html(self):
        found = []
        parser = exporter._PlotDataParser(found.append)
        first = plot_script({'data': []})
        parser.feed(first)
        self.assertEqual(len(found), 1)
        self.assertIsNone(parser.payload)
        parser.feed(first)
        parser.close()
        self.assertEqual(len(found), 2)

    def test_unterminated_chart(self):
        parser = exporter._PlotDataParser(lambda value: None)
        parser.feed('<script class="plot-data" type="application/json">{}')
        with self.assertRaisesRegex(exporter.PowerPointExportError, 'Unterminated'):
            parser.close()

    def test_title_sanitization(self):
        self.assertEqual(exporter._plain_title(
            '&lt;b&gt;Hot &amp; cold&lt;/b&gt;<br>sensor<script>alert(1)</script>'
            '<style>hidden</style>\x00'), 'Hot & cold sensor')
        self.assertLessEqual(len(exporter._short_title('a' * 500)), 160)

    def test_import_is_lazy_and_missing_pptx_is_clear(self):
        original = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name.split('.')[0] in ('pptx', 'plotly', 'PIL'):
                raise ImportError('blocked optional dependency')
            return original(name, *args, **kwargs)

        spec = importlib.util.spec_from_file_location('isolated_exporter', exporter.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch('builtins.__import__', side_effect=blocked):
            spec.loader.exec_module(module)
            with self.assertRaisesRegex(module.PowerPointExportError, 'requires python-pptx') as caught:
                module._dependencies()
            self.assertIn(f'"{module.sys.executable}" -m pip install python-pptx', str(caught.exception))
            with patch.object(module.sys, 'frozen', True, create=True):
                with self.assertRaisesRegex(module.PowerPointExportError, 'Rebuild the dashboard package'):
                    module._dependencies()


@unittest.skipUnless(all(importlib.util.find_spec(name) is not None for name in ('pptx', 'plotly', 'PIL')),
                     'Install python-pptx, Plotly and Pillow in the main environment')
class PowerPointExportTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        from pptx import Presentation

        self.Presentation = Presentation
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'report.html'
        self.output = self.root / 'report.pptx'
        self.calls = []

        def fake_image(figure, **kwargs):
            self.calls.append((figure, kwargs))
            stream = io.BytesIO()
            Image.new('RGB', (kwargs['width'], kwargs['height']), 'white').save(stream, format='PNG')
            return stream.getvalue()

        self.render_patch = patch('plotly.graph_objects.Figure.to_image', autospec=True, side_effect=fake_image)
        self.render = self.render_patch.start()
        self.addCleanup(self.render_patch.stop)

    def write_report(self, *figures):
        self.source.write_text('<html><script>ordinaryJS()</script>'
                               + ''.join(plot_script(fig) for fig in figures) + '</html>', encoding='utf-8')

    @staticmethod
    def texts(slide):
        return [shape.text for shape in slide.shapes if shape.has_text_frame]

    def assert_no_temporary_output(self):
        self.assertEqual(set(self.root.iterdir()), {self.source, self.output})

    def test_deck_titles_notes_dimensions_and_images(self):
        full_title = 'Diode & sensor ' + 'full metadata title ' * 20
        self.source.write_text(
            '<script>not JSON</script>'
            + plot_script({'data': [], 'layout': {
                'width': 1500, 'height': 850,
                'title': {'text': 'Short plot title'},
                'meta': {'export_title': '<b>' + full_title + '</b><script>hidden()</script>'}}},
                'class="extra plot-data" type="application/json"')
            + plot_script({'data': [], 'layout': {'title': {'text': 'Fallback &amp; title<br><sup>Details</sup>'}}},
                          'type="application/json" class="plot-data extra"'), encoding='utf-8')
        result = exporter.export_report_pptx(self.source, title='<b>Thermal &amp; Report</b><script>bad()</script>')
        self.assertEqual(result, self.output)
        deck = self.Presentation(result)
        self.assertEqual(len(deck.slides), 2)
        self.assertAlmostEqual(deck.slide_width / deck.slide_height, 16 / 9, places=6)
        self.assertAlmostEqual(deck.slide_height / 914400, 7.5)
        cover = '\n'.join(self.texts(deck.slides[0]))
        self.assertIn('Thermal & Report', cover)
        self.assertIn('Report source: report.html', cover)
        self.assertRegex(cover, r'Report date: \d{4}-\d{2}-\d{2}')
        self.assertIn('charts are not editable', cover)
        self.assertNotIn('bad()', cover)
        slide = deck.slides[1]
        self.assertEqual(slide.notes_slide.notes_text_frame.text, full_title.strip() + '\n\nFallback & title Details')
        self.assertEqual(self.texts(slide)[0], 'Plots')
        self.assertIn(exporter._short_title(full_title.strip()), self.texts(slide))
        self.assertIn('Fallback & title Details', self.texts(slide))
        pictures = [shape for shape in slide.shapes if shape.shape_type == 13]
        self.assertEqual(len(pictures), 2)
        self.assertLess(pictures[0].left + pictures[0].width, pictures[1].left)
        for picture, (figure, kwargs) in zip(pictures, self.calls):
            self.assertAlmostEqual(picture.width / picture.height, kwargs['width'] / kwargs['height'], places=5)
            self.assertEqual((picture.crop_left, picture.crop_top, picture.crop_right, picture.crop_bottom),
                             (0, 0, 0, 0))
            self.assertGreaterEqual(picture.top, int(1.32 * 914400))
            self.assertLessEqual(picture.top + picture.height, int(6.98 * 914400))
            self.assertEqual(str(slide.background.fill.fore_color.rgb), 'FFFFFF')
            self.assertEqual(figure.layout.width, kwargs['width'])
            self.assertEqual(figure.layout.height, kwargs['height'])
        self.assertEqual(self.calls[0][1], dict(format='png', width=1500, height=850, scale=1))
        self.assertEqual(self.calls[1][1], dict(format='png', width=1600, height=900, scale=1))
        self.assert_no_temporary_output()

    def test_custom_destination_and_default_title(self):
        self.write_report({'data': []})
        target = self.root / 'custom.pptx'
        self.assertEqual(exporter.export_report_pptx(str(self.source), str(target)), target)
        deck = self.Presentation(target)
        self.assertIn('report', self.texts(deck.slides[0]))
        self.assertIn('Chart 1', self.texts(deck.slides[1]))
        self.assertFalse(self.output.exists())

    def test_diode_snapshots_include_heading_limits_and_fill_grid(self):
        figure = {'data': [{'type': 'bar', 'x': [99, 100], 'y': [2, 5]}], 'layout': {
            'height': 440, 'title': {'text': 'Reading distribution'},
            'meta': {'report_product': 'HX28C', 'limit_product': 'NVL_Class_HX28C',
                     'lcl': 95, 'ucl': 105, 'export_title': 'HX28C \u2014 CPU & diode'}}}
        self.write_report(*[figure] * 4)
        exporter.export_report_pptx(self.source)
        slide = self.Presentation(self.output).slides[1]
        pictures = [shape for shape in slide.shapes if shape.shape_type == 13]
        self.assertEqual(len(pictures), 4)
        self.assertEqual(len([shape for shape in slide.shapes if shape.has_text_frame]), 4)
        for picture, (rendered, options) in zip(pictures, self.calls):
            self.assertEqual((options['width'], options['height']), (1200, 500))
            self.assertIn('<b>CPU &amp; diode</b>', rendered.layout.title.text)
            self.assertIn('HX28C | Limit product: NVL_Class_HX28C | LCL: 95 | UCL: 105',
                          rendered.layout.title.text)
            self.assertIn('Reading distribution', rendered.layout.title.text)
            self.assertEqual(rendered.layout.title.y, .92)
            self.assertEqual(rendered.layout.margin.t, 155)
            self.assertEqual(list(rendered.data[0].y), [2, 5])
            self.assertGreater(picture.width / 914400, 6)
            self.assertGreater(picture.height / 914400, 2.5)

    def test_one_plot_slide_and_one_table_preserve_all_summary_values(self):
        headers = ['Product_Name', 'Test', 'Limit_Product', 'count', 'units', 'mean',
                   'stddev_samp', 'minimum', 'maximum', 'LCL', 'UCL', 'Below_LCL', 'Above_UCL']
        values = ['HX28C', 'CPU & diode', 'NVL_Class_HX28C', '72', '24', '100.18333333333331',
                  '0.9464537219998266', '98.4', '103.0', '95.0', '105.0', '0', '0']
        from html import escape

        table = ('<h2>Full-data product / test statistics</h2><table><tr>'
                 + ''.join('<th>' + key + '</th>' for key in headers) + '</tr>'
                 + ('<tr>' + ''.join('<td>' + escape(value) + '</td>' for value in values) + '</tr>') * 15
                 + '</table>')
        self.source.write_text(plot_script({'data': []}) * 4 + table, encoding='utf-8')
        exporter.export_report_pptx(self.source)
        deck = self.Presentation(self.output)
        self.assertEqual(len(deck.slides), 3)
        pictures = [shape for shape in deck.slides[1].shapes if shape.shape_type == 13]
        self.assertEqual(len(pictures), 4)
        for index, picture in enumerate(pictures):
            self.assertGreaterEqual(picture.left, 0)
            self.assertLessEqual(picture.left + picture.width, deck.slide_width)
            self.assertGreaterEqual(picture.top, int(1.32 * 914400))
            self.assertLessEqual(picture.top + picture.height, int(6.98 * 914400))
            for other in pictures[index + 1:]:
                self.assertTrue(picture.left + picture.width <= other.left or
                                other.left + other.width <= picture.left or
                                picture.top + picture.height <= other.top or
                                other.top + other.height <= picture.top)
        slide = deck.slides[2]
        self.assertIn('Data summary', self.texts(slide))
        tables = [shape for shape in slide.shapes if shape.has_table]
        self.assertEqual(len(tables), 1)
        shape = tables[0]
        self.assertLess(shape.top + shape.height, int(7.14 * 914400))
        self.assertEqual([[cell.text for cell in row.cells] for row in shape.table.rows],
                         [headers, *[values] * 15])

    def test_stream_reads_are_bounded(self):
        self.write_report(*[{'data': []}] * 3)
        original_open = Path.open
        source = self.source

        class Guard(io.StringIO):
            def read(self, size=-1):
                self.assert_bounded(size)
                return super().read(size)

        content = self.source.read_text(encoding='utf-8')
        guard = Guard(content)
        guard.assert_bounded = lambda size: self.assertTrue(0 < size <= exporter._READ_SIZE)

        def guarded_open(path, *args, **kwargs):
            return guard if path == source else original_open(path, *args, **kwargs)

        with patch.object(Path, 'open', guarded_open):
            exporter.export_report_pptx(self.source)
        self.assertEqual(len(self.calls), 3)

    def test_no_charts_never_creates_empty_deck(self):
        self.source.write_text('<script>ordinaryJS()</script>', encoding='utf-8')
        with self.assertRaisesRegex(exporter.PowerPointExportError, 'No charts'):
            exporter.export_report_pptx(self.source)
        self.assertEqual(list(self.root.iterdir()), [self.source])

    def test_malformed_json_preserves_existing_output(self):
        self.source.write_text(plot_script({'data': []})
                               + '<script class="plot-data" type="application/json">{broken}</script>', encoding='utf-8')
        self.output.write_bytes(b'previous deck')
        with self.assertRaisesRegex(exporter.PowerPointExportError, 'Chart 2: malformed'):
            exporter.export_report_pptx(self.source)
        self.assertEqual(self.output.read_bytes(), b'previous deck')
        self.assert_no_temporary_output()

    def test_chrome_failure_preserves_output(self):
        self.write_report({'data': []})
        self.output.write_bytes(b'previous deck')
        self.render.side_effect = RuntimeError('Chrome not found')
        with self.assertRaisesRegex(exporter.PowerPointExportError, 'installed Chrome'):
            exporter.export_report_pptx(self.source)
        self.assertEqual(self.output.read_bytes(), b'previous deck')
        self.assert_no_temporary_output()

    def test_save_and_replace_failures_cleanup_and_preserve_output(self):
        from pptx.presentation import Presentation

        self.write_report({'data': []})
        self.output.write_bytes(b'previous deck')

        def fail_save(deck, stream):
            stream.write(b'partial output')
            raise OSError('disk full')

        for failure in (patch.object(Presentation, 'save', fail_save),
                        patch.object(exporter.os, 'replace', side_effect=OSError('locked destination'))):
            with self.subTest(failure=failure), failure:
                with self.assertRaises(exporter.PowerPointExportError):
                    exporter.export_report_pptx(self.source)
                self.assertEqual(self.output.read_bytes(), b'previous deck')
                self.assert_no_temporary_output()

    def test_success_atomically_replaces_previous_deck(self):
        self.write_report({'data': []})
        self.output.write_bytes(b'previous deck')
        exporter.export_report_pptx(self.source)
        self.assertEqual(len(self.Presentation(self.output).slides), 2)
        self.assert_no_temporary_output()

    def test_source_cannot_be_destination(self):
        self.write_report({'data': []})
        original = self.source.read_bytes()
        with self.assertRaisesRegex(exporter.PowerPointExportError, 'source report'):
            exporter.export_report_pptx(self.source, self.source)
        self.assertEqual(self.source.read_bytes(), original)

    def test_template_applied_before_atomic_commit_and_failure_preserves_output(self):
        self.write_report({'data': []})
        template = self.root / 'PPT_Template.pptx'
        template.write_bytes(b'protected template')
        self.output.write_bytes(b'previous deck')
        with patch.object(exporter, '_apply_powerpoint_template',
                          side_effect=exporter.PowerPointExportError('Template unavailable')) as apply:
            with self.assertRaisesRegex(exporter.PowerPointExportError, 'Template unavailable'):
                exporter.export_report_pptx(self.source, template_path=template)
            self.assertEqual(apply.call_args.args[1], template)
            self.assertEqual(self.output.read_bytes(), b'previous deck')
        with patch.object(exporter, '_apply_powerpoint_template') as apply:
            exporter.export_report_pptx(self.source, template_path=template)
            apply.assert_called_once()
        self.assertEqual(template.read_bytes(), b'protected template')
        self.assertEqual(set(self.root.iterdir()), {self.source, self.output, template})
        with self.assertRaisesRegex(exporter.PowerPointExportError, 'overwrite the template'):
            exporter.export_report_pptx(self.source, template, template_path=template)


if __name__ == '__main__':
    unittest.main()