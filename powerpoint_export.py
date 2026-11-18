"""Export report charts as non-editable PNG slides, without reading whole HTML files.

HTML and figure working memory is bounded by one chart plus a read chunk. There
is no chart-count cap; python-pptx necessarily retains the deck/images in memory.
Optional python-pptx, Plotly, Pillow and Kaleido dependencies are loaded on use.
Chrome must already be installed; this module never downloads a browser.
"""

import io
import math
import os
import re
import sys
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path


class PowerPointExportError(RuntimeError):
    """A report could not be exported; any previous destination is preserved."""


_READ_SIZE = 64 * 1024
_BLUE = '183153'
_TEAL = '008C95'


class _TitleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = None

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden = tag
        elif not self.hidden and tag in ('br', 'p', 'div', 'li'):
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag == self.hidden:
            self.hidden = None
        elif not self.hidden and tag in ('p', 'div', 'li'):
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _plain_title(value):
    parser = _TitleText()
    parser.feed(unescape(str(value or '')))
    parser.close()
    text = ''.join(parser.parts)
    # Strip characters that cannot appear in XML 1.0 text (PowerPoint XML).
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]', '', text)
    return ' '.join(text.split())


def _short_title(text):
    return text if len(text) <= 160 else text[:159].rstrip() + '\u2026'


class _PlotDataParser(HTMLParser):
    """Deliver each completed figure synchronously, never collect figures.

HTMLParser normally buffers an entire script (even ignored Plotly JavaScript).
Drain unfinished CDATA after each chunk, retaining only a possible closing tag.
"""

    def __init__(self, consume, consume_summary=None):
        super().__init__(convert_charrefs=True)
        self.consume = consume
        self.payload = None
        self.consume_summary = consume_summary
        self.heading = None
        self.summary_next = False
        self.in_summary = False
        self.headers = []
        self.row = []
        self.cell = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.consume_summary is not None:
            if tag == 'h2':
                self.heading = []
            elif tag == 'table':
                self.in_summary = self.summary_next
                self.summary_next = False
                self.headers = []
            elif self.in_summary and tag == 'tr':
                self.row = []
            elif self.in_summary and tag in ('th', 'td'):
                self.cell = []
        if (tag == 'script' and 'src' not in attrs
                and 'plot-data' in (attrs.get('class') or '').split()
                and (attrs.get('type') or '').strip().lower() == 'application/json'):
            self.payload = io.StringIO()

    def handle_data(self, data):
        if self.payload is not None:
            self.payload.write(data)
        elif self.heading is not None:
            self.heading.append(data)
        elif self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == 'h2' and self.heading is not None:
            self.summary_next = ' '.join(''.join(self.heading).split()).casefold() == 'full-data product / test statistics'
            self.heading = None
        elif self.in_summary:
            if tag in ('th', 'td') and self.cell is not None:
                self.row.append(''.join(self.cell).strip())
                self.cell = None
            elif tag == 'tr' and self.row:
                if not self.headers:
                    self.headers = self.row
                elif len(self.row) != len(self.headers):
                    raise PowerPointExportError('Data summary row does not match its column headers.')
                else:
                    self.consume_summary(self.headers, self.row)
                self.row = []
            elif tag == 'table':
                self.in_summary = False
        if tag == 'script' and self.payload is not None:
            payload, self.payload = self.payload, None
            try:
                self.consume(payload.getvalue())
            finally:
                payload.close()

    def feed(self, data):
        super().feed(data)
        if self.cdata_elem and self.rawdata:
            pending = self.rawdata
            start = pending.rfind('<')
            tail = pending[start:] if start >= 0 else ''
            # Keep only a prefix that could become HTMLParser's CDATA end tag.
            candidate = re.fullmatch(r'</\s*([a-z]*)\s*', tail, re.I)
            possible_end = tail in ('<', '</') or (
                candidate is not None
                and self.cdata_elem.startswith(candidate.group(1).lower()))
            retain = len(tail) if possible_end else 0
            cut = len(pending) - retain
            self.handle_data(pending[:cut])
            self.updatepos(0, cut)
            self.rawdata = pending[cut:]

    def close(self):
        super().close()
        if self.in_summary:
            raise PowerPointExportError('Unterminated full-data statistics table in report HTML.')
        if self.payload is not None:
            self.payload.close()
            self.payload = None
            raise PowerPointExportError('Unterminated .plot-data chart script in report HTML.')


def _dependencies():
    try:
        from pptx import Presentation
        from pptx.dml.color import RGBColor
        from pptx.util import Inches, Pt
    except ImportError as exc:
        recovery = ('Rebuild the dashboard package with python-pptx installed.'
                    if getattr(sys, 'frozen', False) else
                    f'Install it for the running server with: "{sys.executable}" -m pip install python-pptx')
        raise PowerPointExportError(
            f'PowerPoint export requires python-pptx. {recovery}'
        ) from exc
    try:
        import plotly.io as pio
        from PIL import Image
    except ImportError as exc:
        raise PowerPointExportError('PowerPoint export requires Plotly and Pillow.') from exc
    return Presentation, RGBColor, Inches, Pt, pio, Image


_RENDERER_LOCK = threading.Lock()


@contextmanager
def _chart_renderer():
    # One Chrome session for all charts; per-chart launches fail to close reliably on Windows.
    with _RENDERER_LOCK:
        try:
            import kaleido
            kaleido.start_sync_server(silence_warnings=True)
        except Exception:
            kaleido = None
        try:
            yield
        finally:
            if kaleido is not None:
                try:
                    kaleido.stop_sync_server(silence_warnings=True)
                except Exception:
                    pass


def _apply_powerpoint_template(source, template):
    """Use native PowerPoint so protected templates retain their permissions."""
    try:
        import pythoncom
        import win32com.client
        from pptx import Presentation
    except ImportError as exc:
        raise PowerPointExportError('Template export requires Windows, PowerPoint and pywin32.') from exc
    pythoncom.CoInitialize()
    presentation = None
    verification = None
    try:
        generated = Presentation(source)
        app = win32com.client.Dispatch('PowerPoint.Application')
        presentation = app.Presentations.Open(str(Path(template).resolve()), -1, 0, 0)
        protected = bool(presentation.Permission.Enabled)
        if presentation.Slides.Count < 2:
            raise PowerPointExportError('PPT_Template must contain a title slide and a content slide.')
        title_layout = presentation.Slides.Item(1).CustomLayout
        content_layout = presentation.Slides.Item(2).CustomLayout
        body_regions = [shape for shape in content_layout.Shapes
                        if shape.Type == 14 and shape.PlaceholderFormat.Type in (2, 7)]
        if not body_regions:
            raise PowerPointExportError('PPT_Template content layout requires a body placeholder.')
        region = max(body_regions, key=lambda shape: shape.Width * shape.Height)
        bounds = (region.Left, region.Top, region.Width, region.Height)
        theme = content_layout.Design.SlideMaster.Theme
        font = theme.ThemeFontScheme.MinorFont(1).Name
        accent = theme.ThemeColorScheme.Colors(5).RGB
        original_count = presentation.Slides.Count

        with tempfile.TemporaryDirectory(prefix='.ppt-template-', dir=Path(source).parent) as folder:
            for slide_index, original in enumerate(generated.slides, 1):
                layout = title_layout if slide_index == 1 else content_layout
                slide = presentation.Slides.AddSlide(original_count + slide_index, layout)
                title = original.shapes[0].text
                title_shape = slide.Shapes.Title
                title_shape.Width = min(title_shape.Width, presentation.PageSetup.SlideWidth - title_shape.Left - 30)
                if slide_index == 1:
                    parts = title.split(' | ')
                    title_shape.TextFrame.TextRange.Text = '\r'.join(parts[:2])
                    title_shape.TextFrame.TextRange.Font.Size = 28
                    bodies = sorted([shape for shape in slide.Shapes
                                     if shape.Type == 14 and shape.PlaceholderFormat.Type == 2],
                                    key=lambda shape: shape.Top)
                    for index, shape in enumerate(bodies):
                        shape.Width = min(shape.Width, presentation.PageSetup.SlideWidth - shape.Left - 30)
                        shape.TextFrame.TextRange.Text = ('Thermal Analysis Report' if index == 0
                                                         else ' | '.join(parts[2:]))
                    continue
                title_shape.TextFrame.TextRange.Text = title
                for index in range(slide.Shapes.Count, 0, -1):
                    shape = slide.Shapes.Item(index)
                    if shape.Type == 14 and shape.PlaceholderFormat.Type != 1:
                        shape.Delete()
                left, top, width, height = bounds
                scale_x, scale_y = width / 900, height / 406.8
                for shape_index, shape in enumerate(original.shapes):
                    if shape.top < 1.32 * 914400:
                        continue
                    if shape.top + shape.height > 7.0 * 914400:
                        continue
                    target_left = left + (shape.left / 12700 - 28.8) * scale_x
                    target_top = top + (shape.top / 12700 - 95.04) * scale_y
                    target_width = shape.width / 12700 * scale_x
                    target_height = shape.height / 12700 * scale_y
                    if shape.shape_type == 13:
                        image = Path(folder) / f'{slide_index}-{shape_index}.png'
                        image.write_bytes(shape.image.blob)
                        factor = min(target_width / shape.width, target_height / shape.height)
                        image_width, image_height = shape.width * factor, shape.height * factor
                        slide.Shapes.AddPicture(str(image), 0, -1,
                                                target_left + (target_width - image_width) / 2,
                                                target_top + (target_height - image_height) / 2,
                                                image_width, image_height)
                    elif shape.has_table:
                        original_table = shape.table
                        table = slide.Shapes.AddTable(len(original_table.rows), len(original_table.columns),
                                                      target_left, target_top, target_width, target_height).Table
                        for column_index, column in enumerate(original_table.columns, 1):
                            table.Columns.Item(column_index).Width = column.width / 12700 * scale_x
                        for row_index, row in enumerate(original_table.rows, 1):
                            table.Rows.Item(row_index).Height = row.height / 12700 * scale_y
                            for column_index, cell in enumerate(row.cells, 1):
                                target = table.Cell(row_index, column_index).Shape
                                target.TextFrame.MarginLeft = target.TextFrame.MarginRight = 2
                                target.TextFrame.MarginTop = target.TextFrame.MarginBottom = 1
                                text = target.TextFrame.TextRange
                                text.Text = cell.text
                                text.Font.Name = font
                                text.Font.Size = min(10, max(1, target_height / len(original_table.rows) / 3))
                                text.Font.Bold = -1 if row_index == 1 else 0
                                text.Font.Color.RGB = 16777215 if row_index == 1 else 0
                                target.Fill.Solid()
                                target.Fill.ForeColor.RGB = accent if row_index == 1 else 16777215
                    elif shape.has_text_frame:
                        target = slide.Shapes.AddTextbox(1, target_left, target_top, target_width, target_height)
                        target.TextFrame.AutoSize = 0
                        target.TextFrame.MarginLeft = target.TextFrame.MarginRight = 0
                        target.TextFrame.MarginTop = target.TextFrame.MarginBottom = 0
                        target.TextFrame.WordWrap = -1
                        text = target.TextFrame.TextRange
                        text.Text = shape.text
                        text.Font.Name = font
                        text.Font.Size = min(10, max(4, target_height / 2.4))
                        text.Font.Color.RGB = 0
                        target.Left, target.Top = target_left, target_top
                        target.Width, target.Height = target_width, target_height
            for index in range(original_count, 0, -1):
                presentation.Slides.Item(index).Delete()
            result = Path(folder) / 'templated.pptx'
            presentation.SaveCopyAs(str(result), 24)
            presentation.Close()
            presentation = None
            verification = app.Presentations.Open(str(result), -1, 0, 0)
            if protected and not verification.Permission.Enabled:
                raise PowerPointExportError('Template protection was not preserved; export was not published.')
            if verification.Slides.Count != len(generated.slides):
                raise PowerPointExportError('Template export has an unexpected slide count.')
            verification.Close()
            verification = None
            os.replace(result, source)
    except PowerPointExportError:
        raise
    except Exception as exc:
        raise PowerPointExportError(f'PowerPoint could not apply PPT_Template: {exc}') from exc
    finally:
        if verification is not None:
            verification.Close()
        if presentation is not None:
            presentation.Close()
        pythoncom.CoUninitialize()


def export_report_pptx(report_path, destination=None, title=None, template_path=None) -> Path:
    """Write a wide, white-background deck beside the report by default.

Every inline application/json script with the class token ``plot-data`` becomes
an image on one shared plot slide. Existing figure dimensions (notably diode's 1500 x 850) are
retained; otherwise export uses 1600 x 900, scale 1. PNGs are fitted without
cropping. Full sanitized chart titles are stored in speaker notes when supported.
Diode report metadata selects a complete 1200 x 500 snapshot with test heading,
product, limits and chart, replacing the responsive HTML dimensions.
Report date means the source file's local modification date. Replacement is
atomic and occurs only after all figures and the deck have been written.
Full-data product/test statistics become one editable Data summary table,
using the saved HTML values without recomputation.
When template_path is supplied, installed PowerPoint applies its layouts and
preserves its rights-management protection before the output is committed.
"""
    source = Path(report_path)
    output = Path(destination) if destination is not None else source.with_suffix('.pptx')
    temporary = None
    try:
        if source.resolve() == output.resolve() or (
                output.exists() and source.exists() and os.path.samefile(source, output)):
            raise PowerPointExportError('PowerPoint destination must not overwrite the source report.')
        if template_path is not None:
            template_path = Path(template_path).resolve()
            if not template_path.is_file():
                raise PowerPointExportError('PowerPoint template file is missing.')
            if template_path == output.resolve() or (output.exists() and os.path.samefile(template_path, output)):
                raise PowerPointExportError('PowerPoint destination must not overwrite the template.')
        report_date = datetime.fromtimestamp(source.stat().st_mtime).strftime('%Y-%m-%d %H:%M')
        Presentation, RGBColor, Inches, Pt, pio, Image = _dependencies()
        deck = Presentation()
        deck.slide_width = Inches(40 / 3)
        deck.slide_height = Inches(7.5)

        def text_box(slide, text, x, y, w, h, size, color=_BLUE, bold=False):
            shape = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
            frame = shape.text_frame
            frame.word_wrap = True
            frame.margin_left = frame.margin_right = 0
            frame.margin_top = frame.margin_bottom = 0
            paragraph = frame.paragraphs[0]
            paragraph.text = text
            paragraph.font.name = 'Aptos'
            paragraph.font.size = Pt(size)
            paragraph.font.bold = bold
            paragraph.font.color.rgb = RGBColor.from_string(color)
            return shape

        def new_slide(full_title, number):
            slide = deck.slides.add_slide(deck.slide_layouts[6])
            slide.background.fill.solid()
            slide.background.fill.fore_color.rgb = RGBColor(255, 255, 255)
            text_box(slide, _short_title(full_title), .4, .22, 12.5, .88, 22, bold=True)
            # A thin filled textbox avoids extra mandatory shape-enum imports.
            accent = text_box(slide, '', .4, 1.14, 12.5, .04, 1)
            accent.fill.solid()
            accent.fill.fore_color.rgb = RGBColor.from_string(_TEAL)
            accent.line.fill.background()
            text_box(slide, _short_title(_plain_title(source.name)), .4, 7.14, 11.7, .23, 10)
            text_box(slide, str(number), 12.2, 7.14, .7, .23, 10)
            try:
                notes = slide.notes_slide.notes_text_frame
                if notes is not None:
                    notes.text = full_title
            except (AttributeError, NotImplementedError):
                pass  # Notes are not exposed by some python-pptx versions.
            return slide

        report_title = _plain_title(title) or _plain_title(source.stem) or 'Thermal Analysis Report'
        cover = new_slide(report_title, 1)
        text_box(cover, _short_title(report_title), .7, 2.0, 11.9, 1.5, 32, bold=True)
        text_box(cover, f'Report source: {_plain_title(source.name)}\nReport date: {report_date}',
                 .7, 4.0, 11.9, 1.0, 18)
        text_box(cover, 'Charts are embedded PNG images; charts are not editable in PowerPoint.',
                 .7, 5.6, 11.9, .8, 18, color=_TEAL)
        count = 0
        plot_slide = None
        pictures = []
        summary_headers = []
        summary_rows = []

        def consume(payload):
            nonlocal count, plot_slide
            index = count + 1
            try:
                figure = pio.from_json(payload)
            except Exception as exc:
                raise PowerPointExportError(f'Chart {index}: malformed Plotly figure JSON.') from exc
            meta = figure.layout.meta
            full_title = _plain_title(meta.get('export_title')) if isinstance(meta, dict) else ''
            full_title = full_title or _plain_title(figure.layout.title.text) or f'Chart {index}'
            width = figure.layout.width or 1600
            height = figure.layout.height or 900
            snapshot = isinstance(meta, dict) and 'report_product' in meta and 'limit_product' in meta
            if snapshot:
                width, height = 1200, 500
                product = str(meta.get('report_product') or meta.get('source_product') or 'N/A')
                test_title = full_title.removeprefix(product + ' \u2014 ')
                details = ' | '.join((product, f"Limit product: {meta.get('limit_product') or 'N/A'}",
                                      f"LCL: {meta.get('lcl') if meta.get('lcl') is not None else 'N/A'}",
                                      f"UCL: {meta.get('ucl') if meta.get('ucl') is not None else 'N/A'}"))
                caption = _plain_title(figure.layout.title.text) or 'Reading distribution'
                figure.update_layout(
                    title=dict(text=f'<b>{escape(test_title)}</b><br><sup>{escape(details)}</sup>'
                                    f'<br><br>{escape(caption)}',
                               x=.02, xanchor='left', y=.92, yanchor='top', font=dict(size=20)),
                    font=dict(size=18), margin=dict(l=70, r=30, t=155, b=60))
                figure.update_xaxes(tickfont_size=16, title_font_size=18)
                figure.update_yaxes(tickfont_size=16, title_font_size=18)
                figure.update_annotations(font_size=16)
            figure.update_layout(width=width, height=height)
            try:
                png = figure.to_image(format='png', width=width, height=height, scale=1)
            except Exception as exc:
                raise PowerPointExportError(
                    f'Chart {index}: PNG rendering failed. Plotly/Kaleido and an installed Chrome '
                    'browser are required for PowerPoint export. Install Chrome separately; '
                    'no browser is downloaded automatically. Rendering detail: ' + str(exc)
                ) from exc
            with io.BytesIO(png) as image_stream:
                with Image.open(image_stream) as image:
                    pixel_width, pixel_height = image.size
                    image.verify()
                if plot_slide is None:
                    plot_slide = new_slide(full_title, len(deck.slides) + 1)
                image_stream.seek(0)
                picture = plot_slide.shapes.add_picture(image_stream, 0, 0, width=Inches(1))
                pictures.append((picture, full_title, pixel_width, pixel_height, snapshot))
            count += 1

        def consume_summary(headers, values):
            if summary_headers and headers != summary_headers:
                raise PowerPointExportError('Data summary tables have different column headers.')
            if not summary_headers:
                summary_headers.extend(headers)
            summary_rows.append(values)

        parser = _PlotDataParser(consume, consume_summary)
        with _chart_renderer(), source.open('r', encoding='utf-8-sig') as stream:
            while True:
                chunk = stream.read(_READ_SIZE)
                if not chunk:
                    break
                parser.feed(chunk)
            parser.close()
        if not count:
            raise PowerPointExportError('No charts found: report has no inline .plot-data application/json figures.')

        columns = math.ceil(math.sqrt(count))
        rows = math.ceil(count / columns)
        gap = .12
        cell_width = (12.5 - gap * (columns - 1)) / columns
        cell_height = (5.65 - gap * (rows - 1)) / rows
        label_height = min(.45, cell_height / 4) if count > 1 else 0
        if count > 1:
            plot_slide.shapes[0].text_frame.paragraphs[0].text = 'Plots'
            try:
                notes = plot_slide.notes_slide.notes_text_frame
                if notes is not None:
                    notes.text = '\n\n'.join(item[1] for item in pictures)
            except (AttributeError, NotImplementedError):
                pass
        for index, (picture, full_title, pixel_width, pixel_height, snapshot) in enumerate(pictures):
            left = .4 + (index % columns) * (cell_width + gap)
            top = 1.32 + (index // columns) * (cell_height + gap)
            picture_label_height = 0 if snapshot else label_height
            if picture_label_height:
                text_box(plot_slide, _short_title(full_title), left, top, cell_width,
                         picture_label_height, min(10, max(4, picture_label_height * 22)))
            box_width = Inches(cell_width)
            box_height = Inches(cell_height - picture_label_height)
            factor = min(box_width / pixel_width, box_height / pixel_height)
            picture.width = round(pixel_width * factor)
            picture.height = round(pixel_height * factor)
            picture.left = Inches(left) + (box_width - picture.width) // 2
            picture.top = Inches(top + picture_label_height) + (box_height - picture.height) // 2

        if summary_rows:
            slide = new_slide('Data summary', len(deck.slides) + 1)
            table_rows = [summary_headers, *summary_rows]
            row_height = min(1.1, 5.65 / len(table_rows))
            table = slide.shapes.add_table(len(table_rows), len(summary_headers), Inches(.4), Inches(1.32),
                                           Inches(12.5), Inches(row_height * len(table_rows))).table
            weights = [4 if header == 'Test' else 1.5 if header == 'Limit_Product' else 1
                       for header in summary_headers]
            for column, weight in zip(table.columns, weights):
                column.width = Inches(12.5 * weight / sum(weights))
            for row_index, values in enumerate(table_rows):
                for column_index, value in enumerate(values):
                    cell = table.cell(row_index, column_index)
                    cell.text = value
                    cell.text_frame.word_wrap = True
                    cell.margin_top = cell.margin_bottom = Inches(.02)
                    cell.margin_left = cell.margin_right = Inches(.03)
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = RGBColor.from_string(
                        _TEAL if row_index == 0 else 'F0F4F4' if row_index % 2 else 'FFFFFF')
                    for paragraph in cell.text_frame.paragraphs:
                        paragraph.font.name = 'Aptos'
                        paragraph.font.size = Pt(min(10, max(1, row_height * 24)))
                        paragraph.font.bold = row_index == 0
                        paragraph.font.color.rgb = RGBColor.from_string('FFFFFF' if row_index == 0 else _BLUE)

        output.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f'.{output.name}-', suffix='.pptx', dir=output.parent)
        temporary = Path(name)
        with os.fdopen(fd, 'w+b') as stream:
            deck.save(stream)
            stream.flush()
            os.fsync(stream.fileno())
        if template_path is not None:
            _apply_powerpoint_template(temporary, template_path)
        os.replace(temporary, output)
        return output
    except PowerPointExportError:
        raise
    except Exception as exc:
        raise PowerPointExportError(f'PowerPoint export failed: {exc}') from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)