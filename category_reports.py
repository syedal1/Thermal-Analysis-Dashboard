"""Offline category reports; SQL statistics are never calculated from plot samples."""

import csv
import math
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from html import escape
from pathlib import Path

import plotly.graph_objects as go
from plotly.offline import get_plotlyjs
from plotly.subplots import make_subplots
from scipy.stats import t as student_t


_BATCH = 256
_BINS = 40
_DTS_DOMAINS = {'AON': 'SOC'} | {
    f'{domain}S{sensor}': domain
    for domain, count in [('CCF0', 10), ('CCF1', 4), ('CCF2', 10), ('CCF3', 4)] + [
        (f'A{index}', 30) for index in range(4)] + [(f'C{index}', 19) for index in range(8)]
    for sensor in range(count)
}
_QUANTILES = [0, .005, .025, .10, .25, .50, .75, .90, .975, .995, 1]
_KEYS = 'Unit_ID, Test'
_SOCKET = "coalesce(nullif(trim(Socket), ''), 'Unknown socket')"
_HAS_UNIT = r"coalesce(regexp_matches(Unit_ID, '\S'), false)"
_HAS_TEST = r"coalesce(regexp_matches(Test, '\S'), false)"
_VALID_KEYS = f'{_HAS_UNIT} AND {_HAS_TEST}'
_SUMMARY = """count(*) AS observations,
    count(DISTINCT Unit_ID) AS units_with_data,
    count(*) FILTER (WHERE DTSMax < 90) AS under_lt90,
    count(*) FILTER (WHERE DTSMax BETWEEN 90 AND 120) AS within_90_120,
    count(*) FILTER (WHERE DTSMax > 120) AS over_gt120,
    100.0 * count(*) FILTER (WHERE DTSMax < 90) / count(*) AS under_percent,
    100.0 * count(*) FILTER (WHERE DTSMax BETWEEN 90 AND 120) / count(*) AS within_percent,
    100.0 * count(*) FILTER (WHERE DTSMax > 120) / count(*) AS over_percent,
    min(DTSMax) AS min_dtsmax, max(DTSMax) AS max_dtsmax"""


def _text(value):
    return '' if value is None else str(value)


def _selection(store, filters):
    filters = dict(filters or {})
    where, parameters = store._where(filters)
    # ThermalStore treats [] as unrestricted; reports must instead select nothing.
    conditions = ['isfinite(Value)']
    if any(isinstance(value, (list, tuple)) and not value for value in filters.values()):
        conditions.append('FALSE')
    return where + (' AND ' if where else ' WHERE ') + ' AND '.join(conditions), parameters


def _limit(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 20000:
        raise ValueError('plot_limit must be an integer between 1 and 20000')


def _table(stream, connection, title, sql, parameters=()):
    cursor = connection.execute(sql, parameters)
    row_count = 0
    stream.write('<details class="table-section"><summary><h2>' + escape(title)
                 + '</h2></summary><div class="table-wrap"><table><thead><tr>')
    for column in cursor.description:
        stream.write('<th>' + escape(column[0]) + '</th>')
    stream.write('</tr></thead><tbody>')
    while True:
        batch = cursor.fetchmany(_BATCH)
        if not batch:
            break
        row_count += len(batch)
        for row in batch:
            stream.write('<tr>' + ''.join('<td>' + escape(_text(v)) + '</td>' for v in row) + '</tr>')
    stream.write('</tbody></table></div></details>')
    return row_count


def _json(value):
    return (value.replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
            .replace('\u2028', '\\u2028').replace('\u2029', '\\u2029'))


def _figure(stream, figure, variants=None, chart_types=None):
    stream.write('<section class="plot-panel">')
    if variants:
        chart_types = chart_types or {'histogram': 'Histogram', 'scatter': 'Scatter',
                                      'line': 'Line chart', 'distribution': 'Distribution'}
        stream.write('<label class="plot-toolbar">Chart type <select class="chart-type" disabled>'
                     + ''.join(f'<option value="{escape(key)}">{escape(label)}</option>'
                               for key, label in chart_types.items()) + '</select></label>')
    stream.write('<p class="plot-status" role="status">Loading chart…</p>'
                 '<div class="plot"><script class="plot-data" type="application/json">')
    stream.write(_json(figure.to_json()))
    stream.write('</script>')
    if variants:
        for name, variant in variants.items():
            stream.write(f'<script class="plot-option" data-chart-type="{name}" type="application/json">')
            stream.write(_json(variant.to_json()))
            stream.write('</script>')
    stream.write('</div></section>')


_REPORT_SCRIPT = r'''
function sigmaSettings() {
    const control = document.querySelector('#sigma-multiplier');
    if (!control || !control.validity.valid || control.value === '') return null;
    return {multiplier:Number(control.value), show:document.querySelector('#sigma-visible').checked};
}
function applySigma(figure) {
    const settings = sigmaSettings(), statistics = figure.layout.meta?.sigma_statistics;
    if (!settings || !statistics) return;
    figure.layout.meta.sigma_multiplier = settings.multiplier;
    for (const trace of figure.data) {
        if (!trace.meta?.sigma_bound) continue;
        const sign = trace.meta.sigma_bound === 'lower' ? -1 : 1;
        trace.y = statistics.map(row => row.count > 1 && Number.isFinite(row.stddev)
            ? row.mean + sign * settings.multiplier * row.stddev : null);
        trace.visible = settings.show;
        trace.name = (sign < 0 ? 'Calculated lower' : 'Calculated upper') + ' (' + settings.multiplier + ' sigma)';
    }
}
async function renderChart(node) {
    if (node.dataset.rendering) return;
    node.dataset.rendering = 'true';
    const status = node.parentElement.querySelector('.plot-status');
    try {
        if (typeof Plotly === 'undefined') throw new Error('The embedded Plotly library did not load.');
        const payload = node.querySelector('.plot-data');
        const fig = JSON.parse(payload.textContent);
        const variants = {histogram:fig};
        node.querySelectorAll('.plot-option').forEach(option => {
            variants[option.dataset.chartType] = JSON.parse(option.textContent);
            option.remove();
        });
        if (variants.distribution && node.clientWidth < 640) {
            const distribution = variants.distribution;
            distribution.layout.height = 1100;
            distribution.layout.xaxis.domain = [0, 1];
            distribution.layout.xaxis2.domain = [0, 1];
            distribution.layout.yaxis.domain = [.72, 1];
            distribution.layout.yaxis2.domain = [.54, .62];
            distribution.data.find(trace => trace.type === 'table').domain = {x:[0, 1], y:[0, .46]};
        }
        // Retain payload until parsing succeeds; exports read the unchanged disk file.
        payload.remove();
        const initialType = variants.scatter ? 'scatter' : 'histogram';
        const initial = variants[initialType];
        applySigma(initial);
        await Plotly.newPlot(node, initial.data, initial.layout, {responsive:true, displaylogo:false});
        status.hidden = true;
        const selector = node.parentElement.querySelector('.chart-type');
        if (selector) selector.value = initialType;
        if (selector) selector.onchange = async () => {
            selector.disabled = true;
            try {
                const chosen = variants[selector.value];
                applySigma(chosen);
                await Plotly.react(node, chosen.data, chosen.layout, {responsive:true, displaylogo:false});
                status.hidden = true;
            } catch (error) {
                status.hidden = false;
                status.textContent = 'Chart could not be displayed: ' + error.message;
            } finally { selector.disabled = false; }
        };
        if (selector) selector.disabled = false;
        if (initial.layout.meta?.traceability) node.on('plotly_click', event => {
            const detail = event.points[0]?.customdata;
            if (!Array.isArray(detail) || detail.length < 7) return;
            const panel = document.querySelector('.equipment-trace');
            if (!panel) return;
            panel.querySelector('.trace-selection').textContent =
                'Unit: ' + (detail[1] || 'Unavailable') + ' | Sensor: ' + detail[2] + ' | Lot: ' + detail[0] +
                ' | Tester: ' + (detail[3] || 'Unavailable') + ' | Thermal head: ' + (detail[4] || 'Unavailable') +
                ' | Socket: ' + (detail[5] || 'Unknown socket') + ' | Tester site: ' + (detail[6] || 'Unavailable');
            panel.open = true;
            for (const [selector, value] of [['.trace-tester', detail[3]], ['.trace-head', detail[4]]]) {
                const control = panel.querySelector(selector);
                if (control) { control.value = value || ''; control.dispatchEvent(new Event('change')); }
            }
            panel.scrollIntoView({block:'nearest'});
        });
        if (initial.layout.meta?.sigma_statistics) node.refreshSigma = async () => {
            const chosen = variants[selector?.value || initialType];
            applySigma(chosen);
            await Plotly.react(node, chosen.data, chosen.layout, {responsive:true, displaylogo:false});
        };
    } catch (error) {
        status.hidden = false;
        status.textContent = 'Chart could not be displayed: ' + error.message +
            ' Open the full report in a current browser or regenerate the report.';
        status.classList.add('plot-error');
    }
}
const charts = Array.from(document.querySelectorAll('.plot'));
const equipment = document.querySelector('.equipment-trace');
if (equipment) {
    const rows = Array.from(equipment.querySelectorAll('tbody tr'));
    const headers = Array.from(equipment.querySelectorAll('th'), cell => cell.textContent);
    const controls = [['.trace-tester', 'Unit_Tester_Id'], ['.trace-head', 'Thermal_Head_Id']];
    const filter = () => {
        for (const row of rows) row.hidden = controls.some(([selector, field]) => {
            const value = equipment.querySelector(selector).value;
            return value && row.cells[headers.indexOf(field)].textContent !== value;
        });
        equipment.querySelector('.trace-count').textContent = rows.filter(row => !row.hidden).length + ' equipment groups';
    };
    for (const [selector, field] of controls) {
        const control = equipment.querySelector(selector);
        if (!control) continue;
        const values = [...new Set(rows.map(row => row.cells[headers.indexOf(field)].textContent).filter(Boolean))].sort();
        values.forEach(value => control.add(new Option(value, value)));
        control.addEventListener('change', filter);
    }
    equipment.querySelector('.trace-clear')?.addEventListener('click', () => {
        controls.forEach(([selector]) => { equipment.querySelector(selector).value = ''; });
        equipment.querySelector('.trace-selection').textContent = '';
        filter();
    });
    if (rows.length) filter();
}
const sigmaControl = document.querySelector('#sigma-multiplier');
if (sigmaControl) {
    const table = document.querySelector('.dts-statistics table');
    const headers = table ? Array.from(table.querySelectorAll('th')).map(cell => cell.textContent) : [];
    function updateSigmaTable() {
        const settings = sigmaSettings(), status = document.querySelector('#sigma-status');
        if (!settings) { status.textContent = 'Enter a multiplier from 0 to 10 in steps of 0.1.'; return false; }
        status.textContent = 'Test/socket mean +/- ' + settings.multiplier + ' x sample standard deviation of all readings; not specification limits.';
        for (const row of table?.querySelectorAll('tbody tr') || []) {
            const mean = Number(row.cells[headers.indexOf('Test_mean')].textContent);
            const rawSD = row.cells[headers.indexOf('Test_stddev_samp')].textContent;
            const sd = rawSD === '' ? NaN : Number(rawSD);
            for (const [column, sign] of [['Sigma_LCL', -1], ['Sigma_UCL', 1]]) {
                const value = mean + sign * settings.multiplier * sd;
                row.cells[headers.indexOf(column)].textContent = Number.isFinite(value) ? String(value) : 'N/A';
            }
        }
        return true;
    }
    async function updateSigma() {
        if (!updateSigmaTable()) return;
        document.querySelector('#sigma-download').disabled = true;
        try { for (const chart of charts) if (chart.refreshSigma) await chart.refreshSigma(); }
        catch (error) { document.querySelector('#sigma-status').textContent = 'Chart update failed: ' + error.message; }
        finally { document.querySelector('#sigma-download').disabled = false; }
    }
    sigmaControl.addEventListener('change', updateSigma);
    document.querySelector('#sigma-visible').addEventListener('change', updateSigma);
    document.querySelector('#sigma-download').addEventListener('click', () => {
        if (!updateSigmaTable() || !table) return;
        const quote = text => '"' + text.replaceAll('"', '""') + '"';
        const rows = [['Sigma_multiplier', ...headers], ...Array.from(table.querySelectorAll('tbody tr'), row =>
            [String(sigmaSettings().multiplier), ...Array.from(row.cells, cell => cell.textContent)])];
        const url = URL.createObjectURL(new Blob([rows.map(row => row.map(quote).join(',')).join('\r\n')], {type:'text/csv'}));
        const link = document.createElement('a'); link.href = url; link.download = 'dts-calculated-limits.csv';
        link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
    updateSigmaTable();
}
if (charts.length) renderChart(charts[0]);
if (typeof IntersectionObserver === 'function') {
    const observer = new IntersectionObserver(entries => {
        for (const entry of entries) if (entry.isIntersecting) {
            observer.unobserve(entry.target);
            renderChart(entry.target);
        }
    }, {rootMargin:'400px'});
    charts.slice(1).forEach(node => observer.observe(node));
} else {
    // Older/restricted viewers must not leave all charts as blank placeholders.
    (async () => { for (const node of charts.slice(1)) await renderChart(node); })();
}
document.querySelectorAll('details.socket-result').forEach(socket => {
    const summary = socket.querySelector('summary');
    function updateSocket() {
        summary.title = socket.open ? 'Minimize socket plots' : 'Expand socket plots';
        if (socket.open) requestAnimationFrame(() => window.dispatchEvent(new Event('resize')));
    }
    socket.addEventListener('toggle', updateSocket);
    updateSocket();
});
const testSelection = document.querySelector('.test-selection');
if (testSelection) {
    const toggles = Array.from(testSelection.querySelectorAll('.test-enabled'));
    const toggleAll = testSelection.querySelector('.test-toggle-all');
    const sections = Array.from(document.querySelectorAll('.test-result'));
    const sockets = Array.from(document.querySelectorAll('.socket-result'));
    const statistics = document.querySelector('.test-statistics');
    const rows = Array.from(statistics.querySelectorAll('tbody tr'));
    function updateTests() {
        const enabled = new Set(toggles.filter(toggle => toggle.checked).map(toggle => toggle.value));
        sections.forEach(section => { section.hidden = !enabled.has(section.dataset.test); });
        sockets.forEach(socket => { socket.hidden = !Array.from(socket.querySelectorAll('.test-result')).some(section => !section.hidden); });
        rows.forEach(row => { row.hidden = !enabled.has(row.cells[1].textContent); });
        statistics.hidden = enabled.size === 0;
        document.querySelector('.test-empty').hidden = enabled.size !== 0;
        toggleAll.checked = enabled.size === toggles.length;
        toggleAll.indeterminate = enabled.size > 0 && enabled.size < toggles.length;
        testSelection.querySelector('output').textContent = enabled.size + ' of ' + toggles.length + ' tests enabled';
        window.dispatchEvent(new Event('resize'));
    }
    toggles.forEach(toggle => toggle.addEventListener('change', updateTests));
    toggleAll.addEventListener('change', () => {
        toggles.forEach(toggle => { toggle.checked = toggleAll.checked; });
        updateTests();
    });
    updateTests();
    testSelection.hidden = false;
}
'''


@contextmanager
def _document(destination, title, source_note):
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.category-', suffix='.tmp', dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            stream.write('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                         '<meta name="viewport" content="width=device-width,initial-scale=1"><title>'
                         + escape(title) + '</title><style>'
                         'body{font:15px Segoe UI,sans-serif;margin:32px;color:#182b49;background:#f7f9fc}'
                         'h1,h2,h3{overflow-wrap:anywhere}table{border-collapse:collapse;background:white}'
                         'td,th{padding:8px;border:1px solid #d8e1ef;text-align:left}'
                         '.table-wrap{overflow:auto;max-height:600px}.plot{min-height:420px;margin:20px 0}'
                         '.recipe-summary td{max-width:36rem;min-width:100px;white-space:pre-wrap;overflow-wrap:anywhere}'
                         '.plot-panel{overflow-x:auto;background:white;border-radius:10px;margin:20px 0}'
                         '.plot-toolbar{display:flex;align-items:center;gap:12px;padding:12px 16px}'
                         '.chart-type{font:inherit;padding:6px;max-width:100%}'
                         '.sigma-controls{display:flex;flex-wrap:wrap;gap:16px;align-items:center;border:0;border-block:1px solid #d8e1ef;padding:16px 0;min-width:0}'
                         '.sigma-controls input[type=number]{width:90px;padding:6px;font:inherit}'
                         '.sigma-controls output{flex-basis:100%;overflow-wrap:anywhere}'
                         '.sigma-controls button{padding:8px;font:inherit}'
                         '.equipment-trace{border-block:1px solid #d8e1ef;padding:16px 0;margin-top:24px}'
                         '.equipment-trace summary{cursor:pointer;font-weight:600}'
                         '.trace-controls{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}'
                         '.trace-controls select{font:inherit;max-width:100%;padding:6px}'
                         '.trace-selection{display:block;overflow-wrap:anywhere;margin:12px 0}'
                         '.plot-status{padding:12px;color:#475569}.plot-error{color:#991b1b;background:#fee2e2}'
                         '[hidden]{display:none!important}'
                         '.socket-result{margin:24px 0;border-top:1px solid #d8e1ef}'
                         '.socket-result>summary{cursor:pointer;padding:12px 4px;min-height:24px;overflow-wrap:anywhere}'
                         '.socket-result>summary h2{display:inline;font-size:22px;line-height:1.4;margin:0}'
                         '.socket-result>summary:focus-visible{outline:2px solid #2563eb;outline-offset:2px}'
                         '.table-section{margin:16px 0}'
                         '.table-section>summary{cursor:pointer;padding:10px 0;overflow-wrap:anywhere}'
                         '.table-section>summary h2{display:inline;font-size:18px;margin:0}'
                         '.table-section>summary:focus-visible{outline:2px solid #2563eb;outline-offset:2px}'
                         '.test-selection{min-width:0;border:1px solid #d8e1ef;margin:20px 0;padding:12px}'
                         '.test-selection legend{font-weight:600}.test-selection output{display:block;margin:8px 0}'
                         '.test-options{max-height:260px;overflow:auto;display:grid;gap:8px}'
                         '.test-selection label{display:flex;align-items:flex-start;gap:8px;overflow-wrap:anywhere}'
                         '.test-selection input{flex:none;margin-top:3px}'
                         '</style></head><body><h1>' + escape(title) + '</h1><p>'
                         + escape(_text(source_note)) + '</p>')
            yield stream
            stream.write('<noscript>JavaScript is required to display charts. Summary tables remain available.</noscript>'
                         '<script>' + get_plotlyjs() + '</script><script>\n' + _REPORT_SCRIPT +
                         '</script></body></html>')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def resolve_limit_product(product, limits):
    limits = limits or {}
    name = _text(product).strip().casefold()
    for key, entries in limits.items():
        if isinstance(entries, list) and key.casefold() == name:
            return key
    for alias, target in limits.get('_product_aliases', {}).items():
        if alias.casefold() == name and isinstance(limits.get(target), list):
            return target
    return None


def _spec_limits(product, test, limits, socket=None):
    # Same longest-substring convention as the interactive dashboard, without app imports.
    entries = (limits or {}).get(resolve_limit_product(product, limits), [])
    for entry in sorted(entries, key=lambda item: len(str(item.get('test', ''))), reverse=True):
        key = str(entry.get('test') or '').upper()
        if key and key in _text(test).upper():
            selected = entry
            socket_name = _text(socket).strip().casefold()
            if socket_name:
                for name, override in entry.get('sockets', {}).items():
                    if name.strip().casefold() == socket_name:
                        selected = override
                        break
            values = []
            for name in ('UCL', 'LCL'):
                try:
                    value = float(selected.get(name))
                    values.append(value if math.isfinite(value) else None)
                except (ValueError, TypeError):
                    values.append(None)
            return values
    return None, None


def _diode_stats_sql(where, *, per_sensor=False, per_socket=False, report_product=None):
    keys = 'Product_Name, Test' + (', Socket' if per_socket else '') + (', Measurement_Point' if per_sensor else '')
    replacements = ["coalesce(Socket, '') AS Socket"] if per_socket else []
    if report_product:
        replacements.append('? AS Product_Name')
    source = f"(SELECT * REPLACE ({', '.join(replacements)}) FROM readings{{where}})" if replacements else None
    source = source.format(where=where) if source else f'readings{where}'
    return f'''WITH scaled AS (
        SELECT *, coalesce(nullif(max(abs(Value)) OVER (PARTITION BY {keys}), 0), 1) AS scale
        FROM {source}
    ), moments AS (
        SELECT {keys}, count(*) AS count, count(DISTINCT Unit_ID) AS units,
            greatest(-1.0, least(1.0, avg(Value / scale))) AS mean_scaled,
            least(1.0, stddev_pop(Value / scale)) AS population_sd_scaled,
            stddev_samp(Value / scale) AS sample_sd_scaled,
            min(Value) AS minimum, max(Value) AS maximum, max(scale) AS scale,
            quantile_cont(CASE WHEN scale < 1e-100 THEN Value / scale ELSE Value END,
                          {_QUANTILES}) AS quantiles_work
        FROM scaled GROUP BY {keys}
    ) SELECT {keys}, count, units, mean_scaled * scale AS mean,
        population_sd_scaled * scale AS stddev_pop, minimum, maximum,
        CASE WHEN isfinite(sample_sd_scaled * scale) THEN sample_sd_scaled * scale END AS stddev_samp,
        (sample_sd_scaled / sqrt(count)) * scale AS std_error,
        list_transform(quantiles_work, q -> greatest(minimum, least(maximum,
            q * CASE WHEN scale < 1e-100 THEN scale ELSE 1 END))) AS quantiles,
        scale, mean_scaled, sample_sd_scaled FROM moments'''


def _finite(value):
    """Undefined or unrepresentable statistics must never become JSON infinities."""
    return value if value is not None and math.isfinite(value) else None


def _number(value):
    return 'N/A' if value is None else f'{value:.7g}'


def _short(value, length=110):
    text = _text(value)
    return text if len(text) <= length else text[:length - 1] + '…'


def _diode_panel(connection, selected, parameters, stats, limits, source_note, plot_limit, limit_product=None):
    """One exact-count histogram per product/test, with full-data limit checks."""
    count, low, high = stats['count'], stats['minimum'], stats['maximum']
    scale = stats['scale']
    mean_scaled, sd_scaled = stats['mean_scaled'], stats['sample_sd_scaled']
    resolved_product = resolve_limit_product(limit_product or stats['Product_Name'], limits)
    ucl, lcl = _spec_limits(resolved_product, stats['Test'], limits, socket=stats.get('Socket'))
    below, above = connection.execute(
        f'''SELECT count(*) FILTER (WHERE Value < ?), count(*) FILTER (WHERE Value > ?)
            FROM readings{selected}''',
        [lcl, ucl] + parameters).fetchone()

    bin_sql = '''CASE WHEN ? = ? THEN 0 ELSE least(39, greatest(0,
        floor(((Value / ? - ?) / (? - ?)) * 40)))::INTEGER END'''
    counts = dict(connection.execute(
        f'SELECT {bin_sql} AS bin, count(*) FROM readings{selected} GROUP BY bin ORDER BY bin',
        [low, high, scale, low / scale, high / scale, low / scale] + parameters).fetchmany(_BINS))
    if sum(counts.values()) != count:
        raise RuntimeError('Histogram counts do not match full-data count')

    ci_low = ci_high = None
    if count > 1:
        margin_scaled = float(student_t.ppf(.975, count - 1)) * (sd_scaled / math.sqrt(count))
        ci_low = _finite((mean_scaled - margin_scaled) * scale)
        ci_high = _finite((mean_scaled + margin_scaled) * scale)

    display_scale = scale if scale >= 1e100 or scale < 1e-100 else 1.0
    xlow, xhigh = low / display_scale, high / display_scale
    bins = 1 if low == high else _BINS
    centers = ([xlow] if bins == 1 else
               [(1 - (i + .5) / bins) * xlow + ((i + .5) / bins) * xhigh for i in range(bins)])
    width = (xhigh / bins - xlow / bins) if bins > 1 else max(abs(xlow) * .01, 1.0)
    figure = go.Figure(go.Bar(
        x=centers, y=[counts.get(i, 0) for i in range(bins)], width=width,
        name='Exact readings', marker_color='#168b82', showlegend=False,
        hovertemplate='Reading bin center: %{x:.7g}<br>Count: %{y}<extra></extra>'))

    spec_text = ', '.join(f'{label} = {_number(value)}' for label, value in [('LCL', lcl), ('UCL', ucl)]
                          if value is not None)
    full_title = f"{stats['Product_Name']} — {stats['Test']}" + (f' | {spec_text}' if spec_text else '')
    for value, label, color in [(ucl, 'UCL', '#c2410c'), (lcl, 'LCL', '#2563eb')]:
        if value is not None:
            display_value = _finite(value / display_scale)
            if display_value is not None:
                figure.add_vline(x=display_value, line_color=color, line_dash='dash',
                                 annotation_text=f'{label} = {_number(value)}',
                                 annotation_position='top left' if label == 'UCL' else 'top right')
    axis_title = 'Reading (native test units)' + (f' / {scale:.7g}' if display_scale != 1 else '')
    figure.update_xaxes(title_text=axis_title, automargin=True)
    figure.update_yaxes(title_text='Reading count', rangemode='tozero', automargin=True)
    meta = {key: stats[key] for key in ('count', 'units', 'mean', 'stddev_pop', 'stddev_samp', 'std_error',
                                      'minimum', 'maximum', 'quantiles')}
    meta.update(quantile_probabilities=_QUANTILES, ci95_lower=ci_low, ci95_upper=ci_high,
                ci_method='Student-t, df=N-1', missing_count=None,
                limit_product=resolved_product,
                lcl=lcl, ucl=ucl, below_lcl=below if lcl is not None else None,
                above_ucl=above if ucl is not None else None,
                display_scale=display_scale, source_note=_text(source_note), export_title=full_title)
    figure.update_layout(
        title={'text': 'Reading distribution', 'font': {'size': 18}},
        template='plotly_white', height=440, autosize=True, font={'size': 12},
        margin={'l': 60, 'r': 20, 't': 65, 'b': 65}, meta=meta, bargap=0)
    return figure


def _diode_views(connection, selected, parameters, histogram, plot_limit):
    meta = dict(histogram.layout.meta)
    count = meta['count']
    sample_size = min(count, plot_limit)
    samples = connection.execute(f'''WITH ordered AS (
        SELECT row_number() OVER (ORDER BY source, Lot, Unit_ID, Measurement_Point, rowid) AS observation,
            Value, Lot, Unit_ID, Measurement_Point, Unit_Tester_Id, Thermal_Head_Id, Socket,
            Unit_Tester_Site_Id, source FROM readings{selected}
    ) SELECT observation, Value, Lot, Unit_ID, Measurement_Point, Unit_Tester_Id, Thermal_Head_Id,
             Socket, Unit_Tester_Site_Id FROM ordered WHERE observation IN (
        SELECT 1 + floor(i * ? / ?)::BIGINT FROM range(?) AS positions(i)
    ) ORDER BY observation''', list(parameters) + [count - 1, max(1, sample_size - 1), sample_size]
    ).fetchmany(plot_limit)
    scale = meta['display_scale']
    observations = [row[0] for row in samples]
    values = [row[1] / scale for row in samples]
    customdata = [[_text(value) for value in row[2:]] for row in samples]
    sample_meta = dict(meta, plotted_count=len(samples), observation_order='source, lot, unit, sensor, import row',
                       traceability=True)
    variants = {}
    for name, mode in (('scatter', 'markers'), ('line', 'lines+markers')):
        figure = go.Figure(go.Scatter(
            x=observations, y=values, customdata=customdata, mode=mode,
            marker=dict(color='#168b82', size=6), line=dict(color='#168b82', width=1),
            hovertemplate='Observation: %{x}<br>Reading: %{y:.7g}<br>Lot: %{customdata[0]}'
                          '<br>Unit: %{customdata[1]}<br>Sensor: %{customdata[2]}'
                          '<br>Tester: %{customdata[3]}<br>Thermal head: %{customdata[4]}'
                          '<br>Socket: %{customdata[5]}<br>Tester site: %{customdata[6]}<extra></extra>', showlegend=False))
        figure.update_layout(template='plotly_white', height=440, autosize=True,
                             title=f'{name.title()} ({len(samples):,} / {count:,} readings)',
                             margin=dict(l=60, r=20, t=65, b=65),
                             meta=dict(sample_meta, plot_type=name))
        figure.update_xaxes(title_text='Observation (source / lot / unit order)', automargin=True)
        figure.update_yaxes(title_text=histogram.layout.xaxis.title.text, automargin=True)
        for key, color in (('lcl', '#2563eb'), ('ucl', '#c2410c'), ('mean', '#596579')):
            value = meta[key]
            if value is not None and _finite(value / scale) is not None:
                figure.add_hline(y=value / scale, line_dash='dash', line_color=color,
                                 annotation_text=f'{key.upper()} = {_number(value)}')
        variants[name] = figure

    quantiles = meta['quantiles']
    lower_quartile, median, upper_quartile = [quantiles[index] / scale for index in (4, 5, 6)]
    spread = upper_quartile - lower_quartile
    lower_fence, upper_fence = lower_quartile - 1.5 * spread, upper_quartile + 1.5 * spread
    lower_whisker, upper_whisker, outliers = connection.execute(f'''SELECT
        min(Value) FILTER (WHERE Value / ? >= ?), max(Value) FILTER (WHERE Value / ? <= ?),
        count(*) FILTER (WHERE Value / ? < ? OR Value / ? > ?)
        FROM readings{selected}''', [scale, lower_fence, scale, upper_fence,
                                   scale, lower_fence, scale, upper_fence] + list(parameters)).fetchone()
    distribution = make_subplots(rows=2, cols=2, shared_xaxes=True, column_widths=[.63, .37],
                                 row_heights=[.78, .22], horizontal_spacing=.10, vertical_spacing=.10,
                                 specs=[[{'type': 'xy'}, {'type': 'table', 'rowspan': 2}],
                                        [{'type': 'xy'}, None]])
    distribution.add_trace(go.Bar(histogram.data[0]), row=1, col=1)
    distribution.add_trace(go.Box(q1=[lower_quartile], median=[median], q3=[upper_quartile],
                                  lowerfence=[lower_whisker / scale], upperfence=[upper_whisker / scale],
                                  orientation='h', name='Box plot', boxpoints=False,
                                  marker_color='#168b82', showlegend=False), row=2, col=1)
    outlier_values = [value for value in values if value < lower_fence or value > upper_fence]
    if outlier_values:
        distribution.add_trace(go.Scatter(x=outlier_values, y=[0] * len(outlier_values), mode='markers',
                                          name='Sampled outliers', marker=dict(color='#c2410c', size=5),
                                          showlegend=False), row=2, col=1)
    statistics = [('N', count), ('Units', meta['units']), ('Mean', meta['mean']),
                  ('Std dev', meta['stddev_samp']), ('Std error', meta['std_error']),
                  ('Mean 95% CI lower', meta['ci95_lower']), ('Mean 95% CI upper', meta['ci95_upper']),
                  ('Minimum', meta['minimum']), ('25%', quantiles[4]), ('Median', quantiles[5]),
                  ('75%', quantiles[6]), ('Maximum', meta['maximum']), ('LCL', meta['lcl']),
                  ('UCL', meta['ucl']), ('Below LCL', meta['below_lcl']), ('Above UCL', meta['above_ucl']),
                  ('Outliers (1.5 IQR)', outliers)]
    distribution.add_trace(go.Table(header=dict(values=['Statistic', 'Full data'], fill_color='#e8eeee'),
                                     cells=dict(values=[[item[0] for item in statistics],
                                                        [_number(item[1]) for item in statistics]],
                                                fill_color='white', height=24)), row=1, col=2)
    distribution.update_layout(template='plotly_white', height=620, autosize=True,
                               title='Distribution', bargap=0, margin=dict(l=60, r=20, t=65, b=65),
                               meta=dict(sample_meta, plot_type='distribution', outlier_count=outliers))
    distribution.update_yaxes(title_text='Reading count', rangemode='tozero', row=1, col=1)
    distribution.update_yaxes(showticklabels=False, row=2, col=1)
    distribution.update_xaxes(title_text=histogram.layout.xaxis.title.text, row=2, col=1)
    for key, color in (('lcl', '#2563eb'), ('ucl', '#c2410c')):
        value = meta[key]
        if value is not None and _finite(value / scale) is not None:
            for row in (1, 2):
                distribution.add_vline(x=value / scale, line_dash='dash', line_color=color,
                                       row=row, col=1, exclude_empty_subplots=False)
    variants['distribution'] = distribution
    return variants


def _recipe_table(stream, connection, where, parameters, report_product, per_socket=False):
    keys = ['source', 'Unit_ID', 'Lot', 'Program_Name', 'Product_Name']
    join = ' AND '.join(f'recipe.{key} IS NOT DISTINCT FROM selected.{key}' for key in keys)
    join += " AND coalesce(recipe.Socket, '') = coalesce(selected.Socket, '')"
    keys.append('Socket')
    socket_column = "coalesce(nullif(recipe.Socket, ''), 'Unknown socket') AS Socket," if per_socket else ''
    socket_order = ', Socket' if per_socket else ''
    sql = f'''WITH selected AS (SELECT DISTINCT {', '.join(keys)} FROM readings{where})
        SELECT coalesce(?, recipe.Product_Name) AS Product_Name, recipe.Lot, recipe.Program_Name,
               recipe.Recipe_Field, recipe.Recipe_Revision, {socket_column}
               count(DISTINCT nullif(recipe.Unit_ID, '')) AS Units
        FROM recipe_revisions recipe JOIN selected ON {join}
        GROUP BY ALL ORDER BY Product_Name, Lot, Program_Name, Recipe_Field, Recipe_Revision{socket_order}'''
    stream.write('<section class="recipe-summary">')
    if not _table(stream, connection, 'Recipe Revision', sql, list(parameters) + [report_product]):
        stream.write('<p>No recipe revision data in this selection.</p>')
    stream.write('</section>')


def _equipment_trace(stream, connection, destination, where, parameters):
    filename = 'diode-trace-' + uuid.uuid4().hex + '.csv'
    _csv(connection, destination.parent / filename,
         f'SELECT Test, Measurement_Point, Value, Unit_ID, Lot, Product_Name, Program_Name, '
         f'{_SOCKET} AS Socket, Unit_Tester_Id, Thermal_Head_Id, Unit_Tester_Site_Id, source '
         f'FROM readings{where} ORDER BY Socket, Test, Unit_ID', parameters=parameters)
    stream.write('<details class="equipment-trace"><summary>Tester / thermal-head backtrace</summary>'
                 '<output class="trace-selection" role="status"></output>'
                 f'<p><a href="{filename}" download>Download full reading trace (CSV)</a></p>')
    available = connection.execute(f"SELECT count(*) FROM readings{where} AND "
                                   "(nullif(trim(Unit_Tester_Id), '') IS NOT NULL OR "
                                   "nullif(trim(Thermal_Head_Id), '') IS NOT NULL)", parameters).fetchone()[0]
    if available:
        stream.write('<div class="trace-controls"><label>Tester <select class="trace-tester"><option value="">All testers</option></select></label>'
                     '<label>Thermal head <select class="trace-head"><option value="">All thermal heads</option></select></label>'
                     '<button class="trace-clear" type="button">Clear selection</button></div>'
                     '<output class="trace-count" role="status"></output>')
        _table(stream, connection, 'Full-data equipment statistics',
               f'SELECT Product_Name, Test, {_SOCKET} AS Socket, Lot, Unit_Tester_Id, Thermal_Head_Id, '
               f'Unit_Tester_Site_Id, count(*) AS readings, count(DISTINCT Unit_ID) AS units, '
               f'avg(Value) AS mean, min(Value) AS minimum, max(Value) AS maximum '
               f'FROM readings{where} GROUP BY ALL ORDER BY Socket, Test, Unit_Tester_Id, Thermal_Head_Id', parameters)
    else:
        stream.write('<p>No tester or thermal-head identifiers are available in these source readings.</p>')
    stream.write('</details>')


def export_diode(store, output_folder, *, filters, sensor_config, limits, source_note, plot_limit=5000,
                 limit_product=None, report_product=None, per_socket=False):
    """Export selectable histogram/scatter/line/distribution plots and full-data statistics."""
    _limit(plot_limit)
    destination = Path(output_folder) / 'dashboard.html'
    where, parameters = _selection(store, filters)
    stats = 'diode_stats_' + uuid.uuid4().hex
    group_order = 'Socket, Product_Name, Test' if per_socket else 'Product_Name, Test'
    with store.lock:
        connection = store.connection
        try:
            connection.execute(f'''CREATE TEMP TABLE {stats} AS SELECT
                row_number() OVER (ORDER BY {group_order}) AS group_id,
                NULL::VARCHAR AS limit_product, NULL::DOUBLE AS lcl, NULL::DOUBLE AS ucl,
                NULL::BIGINT AS below_lcl, NULL::BIGINT AS above_ucl,
                * FROM ({_diode_stats_sql(where, per_socket=per_socket, report_product=report_product)})''',
                               ([report_product] if report_product else []) + list(parameters))
            with _document(destination, 'Thermal Diode distribution report', source_note) as stream:
                if not connection.execute(f'SELECT count(*) FROM {stats}').fetchone()[0]:
                    stream.write('<p role="status" class="plot-status">No matching finite diode readings. '
                                 'Check the source TEST_NAME values against Thermal_Diode.json and the '
                                 'selected lot, program and product filters. No chart can be generated '
                                 'from this selection.</p>')
                else:
                    stream.write('<fieldset class="test-selection" hidden><legend>Enabled tests</legend>'
                                 '<label><input type="checkbox" class="test-toggle-all" checked>All tests</label>'
                                 '<output aria-live="polite"></output><div class="test-options">')
                    cursor = connection.execute(f'SELECT DISTINCT Test FROM {stats} ORDER BY Test')
                    while True:
                        tests = cursor.fetchmany(_BATCH)
                        if not tests:
                            break
                        for (test,) in tests:
                            label = escape(_text(test))
                            stream.write(f'<label><input type="checkbox" class="test-enabled" value="{label}" checked>'
                                         f'<span>{label}</span></label>')
                    stream.write('</div></fieldset><p class="test-empty" role="status" hidden>No tests enabled.</p>')
                last = 0
                last_socket = None
                while True:
                    cursor = connection.execute(f'SELECT * FROM {stats} WHERE group_id > ? '
                                                'ORDER BY group_id LIMIT ?', [last, _BATCH])
                    columns = [column[0] for column in cursor.description]
                    groups = cursor.fetchmany(_BATCH)
                    if not groups:
                        break
                    for group in groups:
                        summary = dict(zip(columns, group))
                        last = summary['group_id']
                        selected = where + ' AND Test IS NOT DISTINCT FROM ?'
                        group_parameters = list(parameters) + [summary['Test']]
                        if not report_product:
                            selected += ' AND Product_Name IS NOT DISTINCT FROM ?'
                            group_parameters.append(summary['Product_Name'])
                        if per_socket:
                            selected += " AND coalesce(Socket, '') = ?"
                            group_parameters.append(summary['Socket'])
                            if summary['Socket'] != last_socket:
                                if last_socket is not None:
                                    stream.write('</details>')
                                socket_label = escape(summary['Socket'] or 'Unknown socket')
                                stream.write(f'<details class="socket-result" data-socket="{escape(summary["Socket"])}" open>'
                                             f'<summary title="Minimize socket plots"><h2>Socket: {socket_label}</h2></summary>')
                                last_socket = summary['Socket']
                        figure = _diode_panel(
                            connection, selected, group_parameters,
                            summary, limits, source_note, plot_limit, limit_product or report_product)
                        metadata = figure.layout.meta
                        source_products = []
                        source_cursor = connection.execute(
                            f'SELECT DISTINCT Product_Name FROM readings{selected} ORDER BY Product_Name',
                            group_parameters)
                        while True:
                            source_batch = source_cursor.fetchmany(_BATCH)
                            if not source_batch:
                                break
                            source_products.extend(row[0] for row in source_batch)
                        metadata['source_product'] = source_products[0] if len(source_products) == 1 else None
                        metadata['source_products'] = source_products
                        metadata['report_product'] = report_product or summary['Product_Name']
                        if report_product:
                            metadata['export_title'] = f"{report_product} — {summary['Test']}"
                        if per_socket:
                            metadata['socket'] = summary['Socket']
                            metadata['export_title'] = (f"{report_product or summary['Product_Name']} — {summary['Test']}"
                                                        f" | Socket: {summary['Socket'] or 'Unknown socket'}")
                        values = [metadata[key] for key in (
                            'limit_product', 'lcl', 'ucl', 'below_lcl', 'above_ucl')]
                        connection.execute(
                            f'UPDATE {stats} SET limit_product=?, lcl=?, ucl=?, '
                            'below_lcl=?, above_ucl=? WHERE group_id=?', values + [last])
                        heading = escape(_text(summary['Test']))
                        product_label = escape(_text(report_product or summary['Product_Name']))
                        limit_label = escape(metadata['limit_product'] or 'Not configured')
                        heading_tag = 'h3' if per_socket else 'h2'
                        stream.write(
                            f'<section class="test-result" data-test="{heading}">'
                            f'<{heading_tag}>{heading}</{heading_tag}><p>{product_label} | Limit product: {limit_label}'
                            f' | LCL: {_number(metadata["lcl"])} | UCL: {_number(metadata["ucl"])}</p>')
                        variants = _diode_views(connection, selected, group_parameters, figure, plot_limit)
                        _figure(stream, figure, variants)
                        stream.write('</section>')
                if last_socket is not None:
                    stream.write('</details>')
                stream.write('<section class="test-statistics">')
                socket_column = "coalesce(nullif(Socket, ''), 'Unknown socket') AS Socket, " if per_socket else ''
                _table(stream, connection, 'Full-data product / test statistics',
                          f'SELECT coalesce(?, Product_Name) AS Product_Name, Test, {socket_column}coalesce(limit_product, \'Not configured\') AS Limit_Product, '
                          f'count, units, mean, stddev_samp, minimum, maximum, '
                          f'coalesce(lcl::VARCHAR, \'N/A\') AS LCL, coalesce(ucl::VARCHAR, \'N/A\') AS UCL, '
                          f'coalesce(below_lcl::VARCHAR, \'N/A\') AS Below_LCL, '
                                  f'coalesce(above_ucl::VARCHAR, \'N/A\') AS Above_UCL FROM {stats} ORDER BY group_id',
                              [report_product])
                stream.write('</section>')
                _recipe_table(stream, connection, where, parameters, report_product, per_socket=per_socket)
                _equipment_trace(stream, connection, destination, where, parameters)
        finally:
            connection.execute(f'DROP TABLE IF EXISTS {stats}')
    return destination


def _dts_domain(sensor):
    """Domain mapping from DTS-plotting/DTSDataBOT_rev7.py's sensor table."""
    return _DTS_DOMAINS.get(sensor, 'Unknown')


def export_dts(store, output_folder, *, filters=None, sensor_config=None, limits=None,
               source_note=None, plot_limit=5000, limit_product=None, report_product=None, per_socket=False):
    _limit(plot_limit)
    destination = Path(output_folder) / 'dashboard.html'
    where, parameters = _selection(store, filters)
    if sensor_config is not None:
        configured = [name for name, record in sensor_config.items()
                      if 'CATEGORIES' not in record or 'dts' in record['CATEGORIES']]
        if configured:
            where += ' AND Test IN (' + ','.join('?' for name in configured) + ')'
            parameters = list(parameters) + configured
        else:
            where += ' AND FALSE'
    connection = store.connection
    socket_key = "coalesce(Socket, '')" if per_socket else "''"
    groups = connection.execute(f'SELECT DISTINCT Product_Name, Test, {socket_key} AS socket '
                                f'FROM readings{where} ORDER BY socket, Product_Name, Test', parameters).fetchall()
    with _document(destination, 'DTS variability report', source_note) as stream:
        stream.write('<fieldset class="sigma-controls"><legend>Calculated limits</legend>'
                 '<label>Sigma multiplier <input id="sigma-multiplier" type="number" min="0" max="10" step="0.1" value="3" required></label>'
                 '<label><input id="sigma-visible" type="checkbox" checked> Show calculated limits</label>'
                 '<button id="sigma-download" type="button">Download calculated limits</button>'
                 '<output id="sigma-status" role="status"></output></fieldset>')
        last_socket = None
        summary_queries = []
        summary_parameters = []
        for product, test, socket in groups:
            if per_socket and socket != last_socket:
                if last_socket is not None:
                    stream.write('</details>')
                stream.write(f'<details class="socket-result" data-socket="{escape(socket)}" open>'
                             f'<summary title="Minimize socket plots"><h2>Socket: {escape(socket or "Unknown socket")}</h2></summary>')
                last_socket = socket
            selected = where + ' AND Product_Name IS NOT DISTINCT FROM ? AND Test IS NOT DISTINCT FROM ?'
            selected_parameters = list(parameters) + [product, test]
            if per_socket:
                selected += " AND coalesce(Socket, '') = ?"
                selected_parameters.append(socket)
            resolved = resolve_limit_product(limit_product or report_product or product, limits)
            ucl, lcl = _spec_limits(resolved, test, limits, socket=socket if per_socket else None)
            statistics = connection.execute(f'''SELECT Measurement_Point, count(*), avg(Value),
                min(Value), max(Value), stddev_samp(Value) FROM readings{selected}
                GROUP BY Measurement_Point ORDER BY Measurement_Point''', selected_parameters).fetchall()
            total = sum(row[1] for row in statistics)
            test_mean, test_sd = connection.execute(
                f'SELECT avg(Value), stddev_samp(Value) FROM readings{selected}', selected_parameters).fetchone()
            sigma_lower = _finite(test_mean - 3 * test_sd) if test_sd is not None else None
            sigma_upper = _finite(test_mean + 3 * test_sd) if test_sd is not None else None
            sample = connection.execute(f'''SELECT Measurement_Point, Value, Unit_ID, Lot,
                Socket, Thermal_Head_Id, Unit_Tester_Id, Unit_Tester_Site_Id FROM (
                SELECT *, row_number() OVER (ORDER BY Measurement_Point, Unit_ID, Lot, source, Value) AS sample_index
                FROM readings{selected}) WHERE (sample_index - 1) % ? = 0 ORDER BY sample_index LIMIT ?''',
                selected_parameters + [max(1, math.ceil(total / plot_limit)), plot_limit]).fetchall()
            title = f'{report_product or product} | {test}' + (f' | Socket: {socket or "Unknown socket"}' if per_socket else '')
            equipment_hover = ('<br>Socket: %{customdata[3]}<br>Thermal head: %{customdata[4]}'
                               '<br>Tester: %{customdata[5]}<br>Tester site: %{customdata[6]}<extra></extra>')
            figure = go.Figure(go.Scatter(
                x=[_dts_domain(row[0]) for row in sample], y=[row[1] for row in sample],
                mode='markers', name='Readings', marker={'size': 6, 'opacity': .55, 'color': '#167d9a'},
                customdata=[[row[0], row[2], row[3], *row[4:]] for row in sample],
                hovertemplate='Domain: %{x}<br>Sensor: %{customdata[0]}<br>Value: %{y}<br>Unit: %{customdata[1]}<br>Lot: %{customdata[2]}' + equipment_hover))
            figure.update_layout(template='plotly_white', height=520, margin={'t': 75, 'b': 100, 'l': 50, 'r': 20},
                                 legend={'orientation': 'h', 'x': 0, 'y': 1.2, 'font': {'size': 11}},
                                 xaxis={'title': 'Domain', 'type': 'category'}, yaxis_title='Value',
                                 meta={'export_title': title, 'source_product': product, 'report_product': report_product or product,
                                       'socket': socket, 'lcl': lcl, 'ucl': ucl, 'limit_product': resolved,
                                       'sigma_multiplier': 3,
                                       'sigma_basis': 'all_readings_per_test_socket',
                                       'sigma_statistics': [{'sensor': row[0], 'count': total, 'mean': test_mean,
                                                             'stddev': test_sd} for row in statistics],
                                       'total_readings': total, 'sampled_readings': len(sample),
                                       'plot_reference': 'DTSDataBOT_rev7: Value by Domain, TEST_NAME, Sensor labels'})
            for bound, label in ((lcl, 'LCL'), (ucl, 'UCL')):
                if bound is not None:
                    figure.add_hline(y=bound, line_color='#b33c37', line_dash='dash', annotation_text=label)
            sensor_figure = go.Figure(figure)
            sensor_figure.data[0].x = [row[0] for row in sample]
            sensor_figure.data[0].hovertemplate = 'Sensor: %{x}<br>Value: %{y}<br>Unit: %{customdata[1]}<br>Lot: %{customdata[2]}' + equipment_hover
            sensor_figure.add_trace(go.Scatter(x=[row[0] for row in statistics], y=[row[2] for row in statistics],
                                              mode='markers', name='Full-data sensor mean',
                                              marker={'symbol': 'diamond', 'size': 8, 'color': '#cc7a14'}))
            sensor_figure.update_xaxes(title='Sensor', tickangle=-45)
            for view, axes in ((figure, [_dts_domain(row[0]) for row in statistics]),
                               (sensor_figure, [row[0] for row in statistics])):
                for sign, bound, color in ((-1, 'lower', '#087f62'), (1, 'upper', '#a26515')):
                    view.add_trace(go.Scatter(
                        x=axes, y=[sigma_lower if sign < 0 else sigma_upper for row in statistics],
                        mode='markers', name=f'Calculated {bound} (3 sigma)',
                        marker={'symbol': 'line-ew-open', 'size': 14, 'color': color},
                        customdata=[[row[0], total] for row in statistics], meta={'sigma_bound': bound},
                        hovertemplate='Sensor: %{customdata[0]}<br>Calculated limit: %{y}<br>N: %{customdata[1]}<extra></extra>'))
            stream.write(f'<h3>{escape(_text(test))}</h3><p>{escape(_text(report_product or product))}'
                         f' | LCL: {_number(lcl)} | UCL: {_number(ucl)}'
                         f' | Readings: {total} | Plotted: {len(sample)}</p>')
            _figure(stream, figure, {'scatter': sensor_figure}, {'histogram': 'By domain', 'scatter': 'By sensor'})
            summary_queries.append(f'''SELECT ? AS Product_Name, ? AS Test, ? AS Socket,
                Measurement_Point AS Sensor, ? AS LCL, ? AS UCL,
                count(*) AS count, count(DISTINCT Unit_ID) AS units, avg(Value) AS mean,
                stddev_samp(Value) AS stddev_samp, min(Value) AS minimum, max(Value) AS maximum,
                ? AS Test_count, ? AS Test_mean, ? AS Test_stddev_samp,
                ? AS Sigma_LCL, ? AS Sigma_UCL,
                CASE WHEN ? IS NULL THEN NULL ELSE count(*) FILTER (WHERE Value < ?) END AS Below_LCL,
                CASE WHEN ? IS NULL THEN NULL ELSE count(*) FILTER (WHERE Value > ?) END AS Above_UCL
                FROM readings{selected} GROUP BY Measurement_Point''')
            summary_parameters.extend([report_product or product, test, socket, lcl, ucl,
                                       total, test_mean, test_sd, sigma_lower, sigma_upper,
                                       lcl, lcl, ucl, ucl] + selected_parameters)
        if last_socket is not None:
            stream.write('</details>')
        if summary_queries:
            stream.write('<section class="dts-statistics">')
            _table(stream, connection, 'Full-data sensor statistics',
                   'SELECT * FROM (' + ' UNION ALL '.join(summary_queries) +
                   ') ORDER BY Socket, Product_Name, Test, Sensor', summary_parameters)
            stream.write('</section>')
        if not groups:
            stream.write('<p>No matching DTS readings.</p>')
    return destination


def _verification(connection, winners, where, parameters, per_socket=False):
    keys = _KEYS + (', Socket' if per_socket else '')
    source_keys = _KEYS + (f', {_SOCKET}' if per_socket else '')
    socket_column = f', {_SOCKET} AS Socket' if per_socket else ''
    socket_join = ' AND e.Socket = w.Socket' if per_socket else ''
    sql = f'''WITH expected AS (
        SELECT {_KEYS}{socket_column}, max(Value) AS expected_max FROM readings{where} AND {_VALID_KEYS}
        GROUP BY {source_keys}
    ), differences AS (
        SELECT e.expected_max, w.DTSMax FROM expected e FULL OUTER JOIN {winners} w
        ON e.Unit_ID = w.Unit_ID AND e.Test = w.Test{socket_join}
        WHERE e.expected_max IS DISTINCT FROM w.DTSMax
    ) SELECT (SELECT count(*) FROM expected),
        (SELECT count(*) FROM differences),
        (SELECT count(*) FROM (SELECT {keys} FROM {winners} GROUP BY {keys} HAVING count(*) > 1)),
        (SELECT count(*) FROM {winners} WHERE NOT ({_VALID_KEYS}))'''
    expected, mismatch, duplicates, missing = connection.execute(sql, parameters).fetchone()
    if mismatch or duplicates or missing:
        raise RuntimeError('DTSMax verification failed')
    return {'independent_grouped_max_keys': expected, 'max_or_key_mismatches': mismatch,
            'duplicate_winner_keys': duplicates, 'missing_unit_or_test_keys': missing}


def _csv(connection, path, sql, delimiter=',', parameters=()):
    cursor = connection.execute(sql, parameters)
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream, delimiter=delimiter)
        writer.writerow(column[0] for column in cursor.description)
        while True:
            batch = cursor.fetchmany(_BATCH)
            if not batch:
                break
            writer.writerows(batch)


def export_dtsmon(store, output_folder, *, filters, sensor_config, limits, source_note, plot_limit=5000,
                  per_socket=False, group_by_hardware=False):
    """Export unit/test DTSMax, optionally independently within each socket."""
    _limit(plot_limit)
    destination = Path(output_folder) / 'dashboard.html'
    where, parameters = _selection(store, filters)
    maximum_keys = _KEYS + (f', {_SOCKET}' if per_socket else '')
    sample_groups = 'Socket, Domain' if per_socket else 'Domain'
    if group_by_hardware:
        sample_groups = sample_groups.replace('Domain', 'HardwareDomain')
    winners = 'dtsmon_winners_' + uuid.uuid4().hex
    version = destination.parent / ('dtsmon-' + uuid.uuid4().hex)
    published = False
    with store.lock:
        connection = store.connection
        try:
            # DuckDB TEMP tables use the store's configured memory limit and disk spill directory.
            connection.execute(f'''CREATE TEMP TABLE {winners} AS
                SELECT Unit_ID, Test, Lot, Value AS DTSMax, Measurement_Point AS Hot_Sensor,
                    source, cache_rowid, Product_Name, {_SOCKET} AS Socket,
                    CASE WHEN contains(Test, '::') THEN array_to_string(list_slice(string_split(Test, '::'), 1, -2), '::')
                        ELSE 'Unknown' END AS Domain,
                    CASE WHEN starts_with(Test, 'IPC::') THEN 'CPU'
                        WHEN starts_with(Test, 'IPH::') THEN 'HUB'
                        WHEN starts_with(Test, 'IPG::') THEN 'GT'
                        ELSE 'Unknown' END AS HardwareDomain
                FROM (SELECT *, rowid AS cache_rowid, row_number() OVER (
                    PARTITION BY {maximum_keys} ORDER BY Value DESC, rowid ASC
                ) AS winner_rank FROM readings{where} AND {_VALID_KEYS}) WHERE winner_rank = 1''', parameters)
            verification = _verification(connection, winners, where, parameters, per_socket)
            total = connection.execute(f'SELECT count(*) FROM {winners}').fetchone()[0]
            version.mkdir(parents=True)
            columns = 'Unit_ID, Test, Lot, DTSMax, Hot_Sensor, Domain, Product_Name, source, cache_rowid, Socket'
            if group_by_hardware:
                columns += ', HardwareDomain'
            for name, condition, delimiter in [('all_winners.tsv', '', '\t'),
                                                ('over_details.csv', ' WHERE DTSMax > 120', ','),
                                                ('under_details.csv', ' WHERE DTSMax < 90', ',')]:
                _csv(connection, version / name, f'SELECT {columns} FROM {winners}{condition} '
                     'ORDER BY Socket, Lot, Test, Unit_ID', delimiter)
            with _document(destination, 'DTSMON full report', source_note) as stream:
                key_label = 'Unit_ID + full Test + Socket' if per_socket else 'Unit_ID + full Test'
                stream.write(f'<p>DTSMax is the full-data maximum per {key_label}. '
                             'Repeated readings across that key are combined. Only finite cached readings '
                             'with nonblank unit and test identifiers participate. Lot comes from the winning '
                             'row and is not a grouping key. Ties choose the first cached row in input order. '
                             'This is deterministic for this '
                             'cache, not an acquisition-time ordering. Domains retain the full parent test '
                             'namespace; bare test names use Unknown.</p><p>Under: &lt;90 °C; within: '
                             '90–120 °C inclusive; over: &gt;120 °C. These reference thresholds are fixed, '
                             'not test setpoints or configured specification limits. Summaries, percentages '
                             'and downloads are unsampled. Native offline Plotly replaces reference JMP '
                             'charts; no JMP verification is claimed.</p>')
                for title, groups in [('Per-lot summary', 'Lot'), ('Per-domain summary', 'Domain, Lot'),
                                       ('Full test / lot summary', 'Test, Lot')]:
                    if per_socket:
                        groups = 'Socket, ' + groups
                    _table(stream, connection, title,
                           f'SELECT {groups}, {_SUMMARY} FROM {winners} GROUP BY {groups} ORDER BY {groups}')
                if group_by_hardware:
                    groups = ('Socket, ' if per_socket else '') + 'HardwareDomain, Lot'
                    _table(stream, connection, 'CPU / HUB / GT summary',
                           f'SELECT {groups}, {_SUMMARY} FROM {winners} GROUP BY {groups} ORDER BY {groups}')
                sample = connection.execute(f'''SELECT Test, DTSMax, Unit_ID, Hot_Sensor, Lot, Domain, Socket, HardwareDomain
                    FROM (SELECT *, row_number() OVER (PARTITION BY {sample_groups}
                        ORDER BY hash(Unit_ID, Test, Lot), Test, Lot, Unit_ID) AS sample_rank FROM {winners})
                    ORDER BY sample_rank, {sample_groups}, Test, Lot, Unit_ID LIMIT ?''', [plot_limit]).fetchmany(plot_limit)
                stream.write(f'<h2>Variability charts — DTSMax by domain</h2><p>{len(sample)} displayed / '
                             f'{total} full-data maxima; at most {plot_limit} sampled points across all charts. '
                             'Deterministic domain-round-robin hash sample, not a statistical estimate. '
                             'Domains without sampled points have no chart; see exact domain summaries.</p>')
                domains = {}
                for row in sample:
                    domains.setdefault((row[6] if per_socket else '', row[7] if group_by_hardware else row[5]), []).append(row)
                current_socket = None
                for (socket, domain), rows in sorted(domains.items()):
                    if per_socket and socket != current_socket:
                        if current_socket is not None:
                            stream.write('</details>')
                        current_socket = socket
                        stream.write('<details class="socket-result" open>'
                                     f'<summary title="Minimize socket plots"><h2>Socket: {escape(socket)}</h2></summary>')
                    test_names = list(dict.fromkeys(_text(row[0]) for row in rows))
                    tick_names = test_names[::max(1, (len(test_names) + 11) // 12)]
                    tick_labels = [name.rsplit('::', 1)[-1] for name in tick_names]
                    figure = go.Figure(go.Scatter(
                        x=[escape(_text(row[0])) for row in rows], y=[row[1] for row in rows],
                        customdata=[[escape(_text(v)) for v in row[2:5]] +
                                    ([escape(socket)] if per_socket else []) for row in rows],
                        mode='markers', name='DTSMax', marker={'size': 7, 'opacity': 0.7},
                        hovertemplate='Test: %{x}<br>DTSMax: %{y} °C<br>Unit: %{customdata[0]}'
                                      '<br>Hot sensor: %{customdata[1]}<br>Lot: %{customdata[2]}' +
                                      ('<br>Socket: %{customdata[3]}' if per_socket else '') + '<extra></extra>'))
                    for threshold in (90, 120):
                        figure.add_hline(y=threshold, line_dash='dash', annotation_text=f'{threshold} °C')
                    plot_title = f'{domain} | Socket: {socket}' if per_socket else _text(domain)
                    figure.update_layout(title=escape(plot_title), template='plotly_white',
                                         xaxis_title='Full test instance', yaxis_title='DTSMax (°C)',
                                              xaxis={'type': 'category', 'tickmode': 'array',
                                                  'tickvals': [escape(name) for name in tick_names],
                                                  'ticktext': [escape(label if len(label) <= 30 else
                                                             label[:12] + '...' + label[-15:])
                                                         for label in tick_labels],
                                                  'tickangle': -45, 'tickfont': {'size': 10},
                                                  'automargin': False}, height=480,
                                              margin={'t': 65, 'b': 135, 'l': 60, 'r': 25},
                                     meta={'plot_reference': 'DTSDataBOT_rev7.calculate_dtsmax',
                                         'maximum_keys': ['Unit_ID', 'Test'] + (['Socket'] if per_socket else []),
                                         'socket': socket if per_socket else None,
                                         'export_title': plot_title,
                                         'source_note': source_note})
                    _figure(stream, figure)
                if current_socket is not None:
                    stream.write('</details>')
                socket_column = f'{_SOCKET} AS Socket, ' if per_socket else ''
                socket_group = f'{_SOCKET}, ' if per_socket else ''
                _table(stream, connection, 'Excluded units / keys among cached finite readings',
                       f'SELECT {socket_column}Lot, Unit_ID, Test, Functional_Bin, '
                       f"CASE WHEN NOT ({_HAS_UNIT}) AND NOT ({_HAS_TEST}) "
                       f"THEN 'Missing unit and test' WHEN NOT ({_HAS_UNIT}) "
                       "THEN 'Missing unit' ELSE 'Missing test' END AS Reason, count(*) AS cached_readings "
                       f'FROM readings{where} AND NOT ({_VALID_KEYS}) '
                       f'GROUP BY {socket_group}Lot, Unit_ID, Test, Functional_Bin '
                       f'ORDER BY {socket_group}Lot, Unit_ID, Test', parameters)
                stream.write('<p>Exclusions describe cached finite readings only, not rejected source rows. '
                             'The store drops malformed/nonfinite source readings; units with no valid source '
                             'readings are unavailable. Source-unit counts and original-source completeness '
                             'cannot be inferred.</p><details class="table-section"><summary><h2>Verification</h2></summary>'
                             '<div class="table-wrap"><table><tr><th>Check</th><th>Result</th></tr>')
                for check, value in verification.items():
                    stream.write(f'<tr><td>{check}</td><td>{value}</td></tr>')
                stream.write('</table></div><p>Independent grouped SQL MAX compared against every winner, '
                             'including missing/extra key detection (finite cached readings only).</p>'
                             '</details><h2>Full-data files</h2><ul>')
                for name in ('all_winners.tsv', 'over_details.csv', 'under_details.csv'):
                    stream.write(f'<li><a href="{version.name}/{name}">{name}</a></li>')
                stream.write('</ul>')
            published = True
        finally:
            try:
                connection.execute(f'DROP TABLE IF EXISTS {winners}')
            finally:
                if not published and version.exists():
                    shutil.rmtree(version)
    return destination