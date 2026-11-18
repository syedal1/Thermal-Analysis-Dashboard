"""Write a standalone, disk-backed dashboard snapshot without loading all readings."""

from datetime import datetime
from html import escape
from pathlib import Path

import plotly.graph_objects as go
from plotly.offline import get_plotlyjs


def sensor_figure(frame, test, stats, spec_limits, sensor_order):
    figure = go.Figure(go.Scatter(
        x=frame['Measurement_Point'].tolist(), y=frame['Value'].tolist(),
        mode='markers', marker=dict(size=4, color='blue', opacity=0.6),
        customdata=frame['Unit_ID'].tolist(),
        hovertemplate='VISUAL_ID: %{customdata}<br>Test Result: %{y:.4f}<br>Sensor: %{x}<extra></extra>'))
    mean, deviation = float(stats['mean']), float(stats['std'])
    for value, label, color, dash, position in [
        (mean, f'Mean = {mean:.4f}', 'green', 'dash', 'top left'),
        (mean + 10 * deviation, f'UCL (10\u03c3) = {mean + 10 * deviation:.4f}', 'red', 'dot', 'top left'),
        (mean - 10 * deviation, f'LCL (10\u03c3) = {mean - 10 * deviation:.4f}', 'red', 'dot', 'bottom left'),
        (spec_limits[0], f'Spec UCL = {spec_limits[0]}', '#ff8c00', 'dashdot', 'top right'),
        (spec_limits[1], f'Spec LCL = {spec_limits[1]}', '#ff8c00', 'dashdot', 'bottom right'),
    ]:
        if value is not None:
            figure.add_hline(y=value, line_color=color, line_dash=dash, line_width=1.5,
                             annotation_text=label, annotation_position=position,
                             annotation_font=dict(color=color, size=10))
    figure.update_layout(
        template='plotly', title=dict(text=escape(test), font=dict(size=12)),
        xaxis_title='Sensor', yaxis_title='Test Result', height=330, showlegend=False,
        font=dict(size=10), margin=dict(l=55, r=25, t=45, b=95),
        xaxis=dict(type='category', categoryorder='array', categoryarray=sensor_order,
                   tickmode='array', tickvals=sensor_order, tickangle=90, tickfont=dict(size=8)))
    return figure


def export_dashboard(store, output_folder, product=None, source_note=None, plot_limit=5000,
                     filters=None, sensor_config=None, limits=None):
    from atlas_thermal_dashboard_optimized import find_spec_limits, load_config_from_folder, load_product_limits

    if not 1 <= plot_limit <= 20000:
        raise ValueError('Plot point limit must be between 1 and 20000')
    limits = load_product_limits() if limits is None else limits
    sensor_config = load_config_from_folder() if sensor_config is None else sensor_config
    filters = dict(filters or {})
    if product:
        filters['Product_Name'] = product
    base_filters = dict(filters)
    counts = store.overview(filters)
    if not counts['readings']:
        raise ValueError(f'No readings found for {product or "the selected data"}')
    destination = Path(output_folder) / 'dashboard.html'
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.html.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            stream.write('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                         '<meta name="viewport" content="width=device-width, initial-scale=1">'
                         '<title>Thermal Analysis Dashboard</title><style>'
                         'body{font-family:Segoe UI,sans-serif;margin:24px;color:#212529}'
                         'table{border-collapse:collapse;font-size:13px}td,th{padding:6px;border:1px solid #ddd}'
                         '.table-wrap,.plot-wrap{overflow-x:auto}.plot{min-width:1100px;height:330px}h2,h3{overflow-wrap:anywhere}'
                         '</style></head><body><h1>Thermal Analysis Dashboard</h1>')
            stream.write(f'<p>Snapshot: {datetime.now().isoformat(timespec="seconds")}</p>')
            if source_note:
                stream.write(f'<p>{escape(source_note)}</p>')
            stream.write(f'<p>{counts["readings"]:,} readings; {counts["products"]} products; '
                         f'{counts["tests"]} tests; {counts["units"]:,} units.</p>')
            stream.write(f'<p>Statistics include all matching readings. Charts show up to {plot_limit:,} '
                         'sampled readings per test. Live filters require the running dashboard.</p>')
            for product in store.values('Product_Name', filters):
                stream.write(f'<h2>{escape(product)}</h2>')
                filters = dict(base_filters, Product_Name=product)
                stats = store.test_stats(filters)
                tests = sorted(store.values('Test', filters),
                               key=lambda test: ('DTSCODE_SDS_AVG_PASS' not in test.upper(), test))
                for test in tests:
                    selected = dict(filters, Test=test)
                    frame = store.plot_rows(selected, limit=plot_limit)
                    available = store.values('Measurement_Point', selected)
                    configured = sensor_config.get(test, {}).get('COLUMN_NAMES', [])
                    if isinstance(configured, str):
                        configured = [name.strip() for name in configured.split(',')]
                    sensor_order = list(dict.fromkeys(name for name in configured if name in available))
                    sensor_order.extend(name for name in available if name not in sensor_order)
                    figure = sensor_figure(frame, test, stats.loc[test],
                                           find_spec_limits(product, test, limits), sensor_order)
                    plot_width = max(1100, 80 + 12 * len(sensor_order))
                    payload = figure.to_json().replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
                    stream.write(f'<div class="plot-wrap"><div class="plot" style="min-width:{plot_width}px">'
                                 '<script type="application/json" class="plot-data">' + payload +
                                 '</script></div></div>')
                    stream.write(f'<p>{len(frame):,} displayed / {int(stats.at[test, "count"]):,} readings.</p>')
                    stream.write('<details><summary>Exact lot statistics by BOM / PKG / sensor</summary>')
                    for rows in store.iter_statistics(selected, batch_size=200, by_lot=True):
                        stream.write('<div class="table-wrap">' + rows.to_html(index=False, escape=True) + '</div>')
                    stream.write('</details>')
                stream.write('<h3>Full-data statistics</h3><div class="table-wrap">' +
                             stats.to_html(escape=True) + '</div>')
            stream.write('<script>' + get_plotlyjs() + '</script>')
            stream.write('''<script>
const observer = new IntersectionObserver(entries => {
    for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const container = entry.target;
        observer.unobserve(container);
        const payload = container.querySelector('.plot-data');
        const figure = JSON.parse(payload.textContent);
        payload.remove();
        const chart = document.createElement('div');
        container.appendChild(chart);
        Plotly.newPlot(chart, figure.data, figure.layout, {responsive:true, displaylogo:false});
    }
}, {rootMargin:'400px 0px'});
document.querySelectorAll('.plot').forEach(container => observer.observe(container));
</script></body></html>''')
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination