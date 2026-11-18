"""Bounded, disk-backed analysis view for the thermal dashboard launcher."""

import math
import threading
import uuid
from pathlib import Path
from urllib.parse import urlencode

from dash import Dash, Input, Output, State, dash_table, dcc, html, no_update
from dash.exceptions import PreventUpdate
import dash_bootstrap_components as dbc
from flask import request, send_file
import plotly.graph_objects as go

from large_data_store import FILTER_COLUMNS


def create_app(store, input_folder, config_loader, product_mapping, output_folder, initial_refresh=True):
    app = Dash(__name__, external_stylesheets=[dbc.themes.BOOTSTRAP])
    app.title = "Thermal Analysis | Large Data"
    state = {"busy": False, "ready": False, "revision": 0, "message": "Waiting for import", "color": "secondary"}
    state_lock = threading.Lock()
    controls = [("Product_Name", "PRODUCT"), ("BOM", "BOM"), ("PKG", "PKG"), ("Test", "test_name"),
                ("Measurement_Point", "Sensor"), ("Lot", "Lots"), ("Unit_ID", "Unit")]
    statistic_columns = [{"name": label, "id": column, "type": kind} for column, label, kind in [
        ("Product_Name", "PRODUCT", "text"), ("BOM", "BOM", "text"), ("PKG", "PKG", "text"),
        ("Test", "test_name", "text"), ("Measurement_Point", "Sensor", "text"),
        ("readings", "Readings", "numeric"), ("mean", "Mean", "numeric"), ("stddev", "Std Dev", "numeric"),
        ("minimum", "Min", "numeric"), ("maximum", "Max", "numeric")]]

    def set_message(message):
        with state_lock:
            state["message"] = message

    def refresh_worker():
        try:
            files, skipped = store.refresh(input_folder, config_loader(), product_mapping, progress=set_message)
            total = store.summary()["readings"]
            with state_lock:
                state.update(ready=True, revision=state["revision"] + 1, color="success",
                             message=f"{total:,} readings | {files:,} files | {skipped:,} invalid/non-finite values skipped")
        except Exception as error:
            app.logger.exception("Large-data import failed")
            with state_lock:
                state.update(color="danger", message=f"Import failed: {error}. Previous committed data retained.")
        finally:
            with state_lock:
                state["busy"] = False

    def start_refresh():
        with state_lock:
            if state["busy"]:
                return
            state.update(busy=True, color="info", message="Checking data files...")
        threading.Thread(target=refresh_worker, daemon=True).start()

    def require_ready():
        with state_lock:
            if state["busy"] or not state["ready"]:
                raise PreventUpdate

    metric_style = {"padding": "12px 20px", "borderLeft": "3px solid #0066cc"}
    app.layout = dbc.Container([
        dcc.Store(id="large-revision", data=0),
        dcc.Store(id="large-filters", data={}),
        dcc.Interval(id="large-poll", interval=1500),
        html.Header([
            html.Div([html.H1("Thermal Analysis", className="h3 mb-1"), html.Span("Large Data", className="text-muted")]),
            dbc.Button("Refresh data", id="large-refresh", color="primary", className="align-self-center"),
        ], className="d-flex justify-content-between gap-3 flex-wrap py-4 border-bottom"),
        dbc.Alert(id="large-status", className="mt-3", children="Checking data files...", color="info"),
        dbc.Row([
            dbc.Col([html.Label(label, htmlFor=f"large-{column}", className="form-label"),
                     dcc.Dropdown(id=f"large-{column}", placeholder="All", clearable=True,
                                  multi=column == "Lot")], xs=12, sm=6, lg=3)
            for column, label in controls
        ] + [dbc.Col(dbc.Button("Apply filters", id="large-apply", color="primary", className="mt-4"), xs=12, lg=3)], className="g-3"),
        html.Section([
            html.H2("Lot Comparison", className="h5 mt-4"),
            html.Div(id="large-comparison-status", role="status", className="text-muted small"),
            dcc.Loading(dcc.Graph(id="large-comparison-chart", figure=go.Figure(),
                                  config={"displaylogo": False}, style={"height": "480px"})),
            dash_table.DataTable(
                id="large-comparison", columns=statistic_columns[:5] + [{"name": "Lot", "id": "Lot", "type": "text"}] + statistic_columns[5:],
                data=[], page_action="custom", page_current=0, page_size=50, page_count=1,
                sort_action="custom", sort_mode="single", sort_by=[],
                style_table={"overflowX": "auto"},
                style_cell={"textAlign": "left", "padding": "9px", "minWidth": "90px", "maxWidth": "280px", "whiteSpace": "normal", "overflowWrap": "anywhere"},
                style_header={"fontWeight": "bold", "backgroundColor": "#f1f3f5"},
            ),
        ], className="border-bottom pb-4"),
        html.Div(id="large-metrics", className="d-flex flex-wrap gap-3 py-4"),
        html.Section([
            html.Div([
                dcc.RadioItems(id="large-plot-type", options=[{"label": " Histogram", "value": "histogram"},
                    {"label": " Scatter", "value": "scatter"}, {"label": " Box", "value": "box"}],
                    value="histogram", inline=True, inputStyle={"marginLeft": "12px"}),
                html.Div([html.Label("Point limit", htmlFor="large-point-limit", className="me-2"),
                          dcc.Input(id="large-point-limit", type="number", min=100, max=20000, step=100,
                                    value=5000, debounce=True, style={"width": "100px"})]),
            ], className="d-flex justify-content-between gap-3 flex-wrap"),
            dcc.Loading(dcc.Graph(id="large-chart", figure=go.Figure(), config={"displaylogo": False},
                                  style={"height": "440px"})),
            html.Div(id="large-chart-count", className="text-muted small pb-3"),
        ], className="border-top pt-3"),
        html.Section([
            html.Div([
                html.H2("Exact Sensor Statistics", className="h5 mb-0"),
                html.Div([
                    dbc.Button("Export CSV", id="large-export-csv", color="secondary", outline=True, className="me-2", external_link=True),
                    dbc.Button("Export Parquet", id="large-export-parquet", color="secondary", outline=True, external_link=True),
                ], className="d-flex flex-wrap gap-2"),
            ], className="d-flex justify-content-between align-items-center flex-wrap gap-3 py-3"),
            html.Div(id="large-query-error", role="alert", className="text-danger"),
            dash_table.DataTable(
                id="large-statistics", columns=statistic_columns,
                data=[], page_action="custom", page_current=0, page_size=50, page_count=1,
                sort_action="custom", sort_mode="single", sort_by=[],
                style_table={"overflowX": "auto"},
                style_cell={"textAlign": "left", "padding": "9px", "minWidth": "90px", "maxWidth": "280px", "whiteSpace": "normal", "overflowWrap": "anywhere"},
                style_header={"fontWeight": "bold", "backgroundColor": "#f1f3f5"},
            ),
        ], className="border-top pb-4"),
    ], fluid=True, className="px-3 px-md-4", style={"maxWidth": "1600px", "margin": "auto"})

    @app.callback(Output("large-status", "children"), Output("large-status", "color"),
                  Output("large-refresh", "disabled"), Output("large-revision", "data"),
                  Input("large-poll", "n_intervals"), State("large-revision", "data"))
    def status(_ticks, revision):
        with state_lock:
            return state["message"], state["color"], state["busy"], state["revision"] if state["revision"] != revision else no_update

    @app.callback(Output("large-refresh", "title"), Input("large-refresh", "n_clicks"), prevent_initial_call=True)
    def refresh(_clicks):
        start_refresh()
        return "Refresh requested"

    def register_options(column, parents):
        @app.callback(Output(f"large-{column}", "options"), Input("large-revision", "data"),
                      Input(f"large-{column}", "search_value"),
                      *[Input(f"large-{parent}", "value") for parent in parents], State(f"large-{column}", "value"))
        def options(_revision, search, *values):
            require_ready()
            return store.options(column, dict(zip(parents, values[:-1])), search, values[-1])

    for index, (column, _label) in enumerate(controls):
        register_options(column, [parent for parent, _name in controls[:index]])

    @app.callback(Output("large-filters", "data"), Output("large-statistics", "page_current"),
                  Output("large-comparison", "page_current"),
                  Input("large-apply", "n_clicks"), *[State(f"large-{column}", "value") for column, _label in controls],
                  prevent_initial_call=True)
    def apply_filters(_clicks, *values):
        return {column: value for (column, _label), value in zip(controls, values) if value is not None and value != []}, 0, 0

    @app.callback(Output("large-comparison", "data"), Output("large-comparison", "page_count"),
                  Output("large-comparison-chart", "figure"), Output("large-comparison-status", "children"),
                  Input("large-revision", "data"), Input("large-filters", "data"),
                  Input("large-comparison", "page_current"), Input("large-comparison", "sort_by"))
    def comparison(_revision, filters, page, sort_by):
        require_ready()
        figure = go.Figure()
        figure.update_layout(template="plotly_white", margin={"l": 55, "r": 20, "t": 25, "b": 120},
                             xaxis={"title": "Lot", "type": "category", "automargin": True},
                             yaxis={"title": "Mean +/- population standard deviation", "automargin": True},
                             showlegend=False, uirevision=str(filters))
        if not filters or filters.get("Test") is None:
            return [], 1, figure, "No test selected"
        try:
            rows, pages = store.statistics(filters, page=page or 0, sort_by=sort_by, by_lot=True)
            groups = {}
            for row in rows:
                identity = tuple(row[column] for column in ("Product_Name", "BOM", "PKG", "Test", "Measurement_Point"))
                groups.setdefault(identity, []).append(row)
            palette = ["#0066cc", "#15856b", "#b44d12", "#b32752", "#64701c", "#555555"]
            for index, (identity, group) in enumerate(groups.items()):
                figure.add_trace(go.Scatter(
                    x=[row["Lot"] or "(blank)" for row in group], y=[row["mean"] for row in group], mode="markers",
                    name=" | ".join(value or "(blank)" for value in identity),
                    marker={"size": 10, "color": palette[index % len(palette)], "symbol": index % 12},
                    error_y={"type": "data", "array": [row["stddev"] for row in group], "visible": True},
                    customdata=[[row[column] for column in ("Product_Name", "BOM", "PKG", "Measurement_Point", "readings", "minimum", "maximum")] for row in group],
                    hovertemplate="Lot: %{x}<br>Mean: %{y}<br>PRODUCT: %{customdata[0]}<br>BOM: %{customdata[1]}"
                                  "<br>PKG: %{customdata[2]}<br>Sensor: %{customdata[3]}<br>Readings: %{customdata[4]}"
                                  "<br>Min: %{customdata[5]}<br>Max: %{customdata[6]}<extra></extra>",
                ))
            if not rows:
                return [], pages, figure, "No matching readings"
            missing = [column for column in ("BOM", "PKG") if any(not row[column] for row in rows)]
            caption = f"Exact lot statistics | Page {(page or 0) + 1} of {pages} | {len(rows)} groups"
            if missing:
                caption += " | Missing source metadata: " + ", ".join(missing)
            return rows, pages, figure, caption
        except Exception as error:
            app.logger.exception("Lot comparison failed")
            return [], 1, figure, f"Comparison failed: {error}"

    @app.callback(Output("large-metrics", "children"), Output("large-chart", "figure"), Output("large-chart-count", "children"),
                  Input("large-revision", "data"), Input("large-filters", "data"), Input("large-plot-type", "value"),
                  Input("large-point-limit", "value"))
    def chart(_revision, filters, plot_type, limit):
        require_ready()
        try:
            summary = store.summary(filters)
            sample = store.sample(filters, min(20000, max(100, int(limit or 5000))))
            def metric(label, value):
                text = "-" if value is None or not math.isfinite(value) else f"{value:,.4f}"
                if label == "Readings":
                    text = f"{int(value):,}"
                return html.Div([html.Small(label, className="text-muted"), html.Div(text, className="h4 mb-0")], style=metric_style)
            metrics = [metric(label, summary[key]) for label, key in [
                ("Readings", "readings"), ("Mean", "mean"), ("Std Dev", "stddev"), ("Min", "minimum"), ("Max", "maximum")]]
            figure = go.Figure()
            if plot_type == "scatter":
                figure.add_trace(go.Scattergl(x=sample["Measurement_Point"], y=sample["Value"], mode="markers",
                    marker={"color": "#0066cc", "size": 5, "opacity": 0.6}, customdata=sample[["Test", "Unit_ID", "Lot"]],
                    hovertemplate="%{x}: %{y}<br>Test: %{customdata[0]}<br>Unit: %{customdata[1]}<br>Lot: %{customdata[2]}<extra></extra>"))
            elif plot_type == "box":
                figure.add_trace(go.Box(y=sample["Value"], name="Selected readings", marker_color="#15856b", boxpoints=False))
            else:
                figure.add_trace(go.Histogram(x=sample["Value"], nbinsx=60, marker_color="#0066cc"))
            figure.update_layout(template="plotly_white", margin={"l": 50, "r": 20, "t": 25, "b": 60},
                                 uirevision=str(filters), showlegend=False)
            if sample.empty:
                figure.add_annotation(text="No matching readings", x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False)
            return metrics, figure, f"Chart: {len(sample):,} sampled / {summary['readings']:,} matching readings. Statistics: all matching readings."
        except Exception as error:
            app.logger.exception("Large-data chart query failed")
            return [], go.Figure(), f"Query failed: {error}"

    @app.callback(Output("large-statistics", "data"), Output("large-statistics", "page_count"), Output("large-query-error", "children"),
                  Input("large-revision", "data"), Input("large-filters", "data"), Input("large-statistics", "page_current"),
                  Input("large-statistics", "sort_by"))
    def statistics(_revision, filters, page, sort_by):
        require_ready()
        try:
            rows, pages = store.statistics(filters, page=page or 0, sort_by=sort_by)
            return rows, pages, ""
        except Exception as error:
            app.logger.exception("Large-data statistics query failed")
            return [], 1, f"Query failed: {error}"

    @app.callback(Output("large-export-csv", "href"), Output("large-export-parquet", "href"), Input("large-filters", "data"))
    def export_links(filters):
        query = urlencode(filters or {}, doseq=True)
        return f"/large-export/csv?{query}", f"/large-export/parquet?{query}"

    @app.server.get("/large-export/<file_format>")
    def export(file_format):
        if file_format not in {"csv", "parquet"} or any(key not in FILTER_COLUMNS for key in request.args):
            return "Invalid export request", 400
        with state_lock:
            if state["busy"] or not state["ready"]:
                return "Data import is in progress or has not completed", 409
        folder = Path(output_folder)
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"thermal_{uuid.uuid4().hex}.{file_format}"
        try:
            filters = {key: request.args.getlist(key) for key in request.args}
            store.export(destination, filters)
            return send_file(destination.resolve(), as_attachment=True, download_name=f"thermal_readings.{file_format}")
        except Exception:
            destination.unlink(missing_ok=True)
            app.logger.exception("Large-data export failed")
            return "Export failed. Check server logs and available disk space.", 500

    if initial_refresh:
        start_refresh()
    else:
        state.update(ready=True, revision=1, message="Ready", color="success")
    return app