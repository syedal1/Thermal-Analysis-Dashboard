# Thermal Dashboard Package

## Shared Browser Dashboard

Open the shared folder and run `START_THERMAL_DASHBOARD.bat`, or open
`index.html` directly:

`\\gar.corp.intel.com\ec\proj\mdl\pg\intel\engineering\dev\sctp\axl_user\cheekeon\Thermal-Analysis-Dashboard`

This is the read-only HTML report library, with the approved socket report
selected at startup. Viewing requires a browser and access to the share, not an
EXE or local Python installation. It displays published snapshots; arbitrary
VPO lookup and live data pulls require a separate hosted backend and are not
provided by this static page.

Maintainers run `build_dashboard.ps1` to generate `output/shared`, then
`publish_dashboard.ps1` to copy its HTML and assets to the share.

## Current Folder Layout

This project now lives in `Thermal-Analysis-Dashboard`.
Use `input/` for source files, `config/` for JSON configuration and `output/`
for generated HTML, PDF and PNG. The former names Input_data, Config and
HTML_Output in the historical notes below refer to these renamed folders.

The legacy source dashboard can be started with `run_dashboard.py`. Run
`EXPORT_HTML.bat` to create `output/dashboard.html`, a standalone snapshot with full-data statistics,
lot tables and sampled interactive plots. Stop the live server with Ctrl+C before
exporting, because a database can only be opened by one writer process.
The HTML snapshot does not include the server-backed Dash filter controls.
Regenerate it after changing input/config files.

To export only Cdie_816_N2P from source Python, run:
`python run_dashboard.py --export-html --product NVL_Cdie_816_N2P --export-folder output/Cdie_816_N2P`.
The sensor plots include blue readings, a green mean, red mean +/-10 sample
standard deviations, and orange specification limits from config/Product_Test_Limits.json.
Statistics use all matching readings; plots show up to 5,000 sampled readings.
Configured sensor order is preserved. Tests without configured spec limits show
only the statistical lines. Regenerate and republish HTML to share export changes.

For an explicit cached-data export without reimporting a large workbook:

```powershell
python run_dashboard.py --export-html --cached --product NVL_Cdie_816_N2P --export-folder output/Cdie_816_N2P --memory-limit 1GB --threads 1 --batch-size 5000 --plot-limit 5000
```

The report warns when cached input/configuration fingerprints are stale; current
specification limits are still applied. Omit `--cached` to refresh from source.
`--plot-limit` accepts 1-20,000 points per test without changing full-data statistics.
HTML lot-statistic tables now stream in 200-row batches from a single grouped
query per test, avoiding repeated full-data aggregation for every table page.
Plots initialize as they approach the viewport, with Plotly embedded for offline
use. Wide sensor arrays scroll horizontally to keep vertical labels readable.
The standalone HTML still grows with the number of tests and statistic groups;
use the live dashboard's paginated views for extremely large reports.

All maintained scripts, tests, assets and data folders are in this folder.
Generate HTML with `./build_dashboard.ps1 -Python <path-to-python.exe>` after
installing the requirements. This runs the test gate and publishes snapshots
under `output/shared`; it does not build an executable.

## Run

Double-click `START_THERMAL_DASHBOARD.bat` in the source folder. It starts the live
report portal and opens http://127.0.0.1:8063/ automatically, or reopens the existing
portal for this workspace. Keep its server window open while processing; closing
it stops the service. No administrator rights are needed. The launcher uses the
project `.venv` if present, otherwise `python.exe` on PATH; Python and the packages
in `requirements_dashboard.txt` must already be installed. If port 8063 belongs to
another service or an older dashboard, close that service before starting.
From the published share, where no source exists, the same launcher opens the
read-only HTML library, not the live workspace. New report charts open as scatter by default.
START_PACKAGED_DASHBOARD.bat is a compatibility alias.
No Python installation or running server is needed to view published reports.

For the optional source dashboard, run `python run_dashboard.py`; its default
address is http://127.0.0.1:8050. Use `--port 8051` for a different port.
Only one process may open a given database. Close the console with Ctrl+C to
stop the server. Do not expose this unauthenticated development server to an
untrusted network.

## Report Library

The shared AQUA and TRACE baseline is the report portal at
http://127.0.0.1:8063/. Start it with `START_PROFILE_PORTAL.bat` or the command
below. Both sources use the same report library, report layout, statistics,
publishing and PowerPoint workflow. The original sidebar dashboard on port 8050
is a separate, optional interface, not this baseline.

The interface calls saved configurations **reports**. Existing `Profiles/`
definitions, `input/profiles/` and `Saved_Sessions/profiles/` folders,
`/api/profiles` endpoints, JSON fields, and the `--profile-portal` launcher flag
retain their existing names for compatibility; no data migration is required.

New reports and copies use AQUA (the source control is hidden) and automatically
start one background job for the selected categories and tests. Python connects to the stdio servers in
`.vscode/mcp.json` using the MCP SDK; VS Code does not need to remain open.
Install the updated requirements before starting the source portal. The configured
server executables and their network/authentication dependencies must be available.
`config/aqua_cli.json` selects `transport: mcp`; explicit `cli` remains supported.

AQUA receives up to three attempts in isolated folders, stopping on success.
After failure, Python starts TRACE MCP, obtains its current CLI endpoint, and
retrieves ITUFF-backed results for the report's lots, configured operations and
subflows. Exact lot/operation filters are checked against returned job
metadata. Socket plots use actual metadata, including CLASSHOT, CSM, PHMHOT,
PHMCOLD and PHMROOM; absent metadata remains Unknown socket. Newly generated
charts default to scatter. Existing saved HTML is unchanged until reprocessed.

Completion and failure are saved with the report. The portal polls job status
and sends a browser notification when permission is granted; otherwise its
completion banner remains available. Keep the server running while processing
and a portal tab open for immediate notifications. Closing a tab does not cancel
the Python job; reopening the report shows its saved status. Email is not configured.
Empty categories are reported explicitly, and failed jobs retain the previous snapshot.
Reports use independent processing locks, caches, inputs, and jobs. Creating or
processing another report is allowed while a pull is running. Editing or deleting
the active report is blocked; static index publication uses a separate brief lock.

Lot IDs take priority whenever entered: the test program is ignored in retrieval
and analysis. Without lots, AQUA accepts an exact program such as
`NVLHX66C0H13K0CSX46` or a wildcard such as `NVLHX66C0H13K0*`. Returned program
names are validated. Temporary report exports replace saved lot/program/time
filters; shared parameter exports are never edited. TRACE fallback still requires
lot IDs. CLASS uses `NVL_Aqua_Class_Automation_Parameters.txt`; SORT uses
`NVL_Aqua_Sort_Automation_Parameters.txt`. SDX requires its complete parameter
export for compatibility, so SORT may download additional tests; analysis remains
restricted to the selection. Missing selected subflows produce no matching data,
never invented socket labels.

If live TRACE is unavailable, a matching normalized TRACE export staged within
the last 24 hours remains an optional fallback. Ordinary input files are never
silently reused by a fresh pull. To stage a normalized CSV/TSV/TXT export:

From source Python:

```powershell
python stage_trace_fallback.py <report-slug> <normalized-trace-export.csv> --retrieved-at <ISO-8601-time>
```

The source launcher also supports staging:

```powershell
python run_dashboard.py --stage-trace-fallback <report-slug> --trace-files <normalized-trace-export.csv> --trace-retrieved-at <ISO-8601-time>
```

Staging is a maintainer operation; the static HTML page cannot retrieve new data.

The saved report's effective lot-or-program, product, subflow and domain must match the
export request. The staging command records those filters, timestamp and file
hashes. Only files with that receipt are eligible for AQUA-failure fallback.
Raw TRACE MCP result files must first be normalized to the dashboard's input
columns. A TRACE-sourced fresh pull tries live TRACE before the staged export.

Start the editable report library from source Python:

```powershell
python run_dashboard.py --profile-portal --port 8063 --service-url http://127.0.0.1:8063
```

On **01 Reports**, create an owner profile with **Add owner**, or select an
existing owner. Both report listings and **02 Report Analysis** follow that owner.
Names are folder labels, not authenticated identities. `config/owner_storage.json`
sets the shared reports root to
`\\gar.corp.intel.com\ec\proj\mdl\pg\intel\engineering\dev\sctp\axl_user\cheekeon\Thermal-Analysis-Dashboard\reports`.
New reports are saved under `<reports_root>/<owner>/<report-id>/`, with a profile,
version manifest, source exports, frozen configuration, and generated HTML trees.
Local `Profiles`, input and DuckDB folders remain working indexes/caches; existing
reports are not moved. Share access is required to create owners/reports and save
their results. Copy a report to change its owner; historical snapshots are retained.

**Add report** offers **New report** or **Copy existing**. Select CLASS or SORT,
choose a product and owner profile, then provide a
report name, and at least one lot or test program. Copying prefills settings
but creates a new ID without copying datasets or plots. **Edit** updates the
existing report. **Delete** asks for confirmation and archives its definition
under `Profiles/archive`; source files, caches and reports are retained.
Deletion is not an access-control mechanism for previously shared report files.

Open a report and select **Thermal Diode**, **DTS** or **DTSMON**. All tabs use the same
CLASS/SORT report settings, source folder, domain controls, publishing and
PowerPoint workflow. Thermal Diode uses `config/Thermal_Diode.json` and
the selected report product to combine all selected lots and source-product
identifiers into one Thermal Diode plot per exact test name and CLASS socket.
Lots remain available in point hover and the full trace CSV; they do not create
separate plots. Full-data counts, means, distributions and limit counts use the
combined readings. Without a selected report product, standalone exports keep
different raw products separate. Each processing run reloads
`config/Product_Test_Limits.json`, resolving exact names and explicit aliases
first, then domain-qualified names such as `S8C` to `NVL_Class_S8C`. Socket
overrides remain authoritative; unconfigured limits stay N/A. Reprocess saved
reports to apply code or limit-file changes; previous snapshots remain unchanged.

DTS uses
`config/DTS.json` for exact, case-sensitive test selection and positional sensor names.
Every listed DTS name is eligible, including explicitly listed names outside PTH_DTS
modules; unlisted PTH_DTS tests are not discovered or substituted. If a test belongs
to multiple category files, each category uses its own configured sensor order.
DTSMON uses `config/DTSMON.json` sensor mappings and only accepts
`IPC::ARR_*::*_DTS`, `IPC::FUN_*::*_DTS`, `IPC::SCN_*::*_DTS`, including their
`_DTS_1` and `_DTS_2` variants; `IPH` and `IPG` accept the same ARR/FUN/SCN
patterns with the `_DTS` ending only. Matching is case-sensitive and covers the
whole test name. Other tests containing `DTS` are not automatically assigned to
DTSMON. Newly observed tests matching these patterns are included with their
named sensor labels. Unlisted positional-only tests cannot supply sensor names and
are not added. DTS membership
takes precedence over DTSMON. Only readings matching the effective lot-or-program
and selected subflows participate. Product selection chooses labels and configured
limits from `Product_Test_Limits.json`; it is not a raw-product restriction for
configured product names. Configured positional sensor names are retained;
no sensor mapping is inferred from similar test names.
Named DTS/DTSMON configs use the reference delimiter rules: `sensor:value|...`,
configured positional `value|value` or `value,value`, `sensor=value;...`, and scalars.
Named duplicates keep the last value within a result; unnamed positional extras
are ignored. PARAMETRIC rows use `TEST_RESULT_NUMERIC` when that field exists.
Category checkboxes and domain-specific test lists appear in **Add report** and
**Data and processing**. Choose Thermal Diode, DTS, DTSMON, or any combination;
keep all tests enabled or select individual names/patterns. **Process selection**
uses local report input files. Enable **Pull fresh data** for AQUA retrieval and
processing of that selection. CLASS/SORT applies to the whole job. Selected
CLASSHOT, CSM, PHMHOT, PHMCOLD and PHMROOM subflows are matched to actual source
socket metadata; an empty selection accepts all available subflows.

DTSMON reuses ordered sensor discovery from the `C:\Thermal_DTS\DTS-plotting`
workflow without executing or modifying that project. Each snapshot writes its
generated `config/DTSMON.json`, retaining the original seed. Portal DTSMON charts
group IPC tests as CPU, IPH as HUB, and IPG as GT, separately per CLASS socket.
Full test namespaces remain in tables/downloads. Maxima and statistics use all
selected readings; chart sampling retains the existing point limit.
Reports without matching
processed data display an empty plot state rather than inherited or sample data.

The DTS tab adapts the `DTS-plotting/DTSDataBOT_rev7.py` variability chart to
standalone Plotly HTML: Value by Domain, split by test, with Sensor/Unit/Lot
hover details. **By sensor** also displays full-data sensor means. The reference
sensor-domain mapping is retained (including AON -> SOC); unrecognized sensors
are labeled Unknown. This browser implementation does not launch JMP or execute
the reference script. All three CLASS categories have independently collapsible
socket groups, using explicit Socket metadata or its ProcessStep fallback; missing
metadata appears as Unknown socket. SORT remains combined. DTS and Thermal Diode
limits use the selected product and optional socket
overrides from `config/Product_Test_Limits.json`. Statistics and limit counts use
all matching readings; plots contain at most 5,000 sampled readings per group.
All DTS plots appear first, followed by one combined full-data sensor summary table.

Named DTS runs freeze the original `config/DTS.json` in the version folder and retain
its sensor mappings; observed sensor lists do not replace them. Legacy runs without
a named DTS file retain their generated `config/DTS.generated.json` behavior.
Maintained configs are not modified. Old version configs remain only as immutable
provenance, and failed runs remove their temporary version folder and leave the
previous successful snapshot active. Replace the old input files in the report's
source folder before processing a new dataset; all supported files in that folder
are part of the current run. Processing publishes a new latest snapshot for the
entire report (Thermal Diode, DTS and DTSMON); earlier snapshot versions remain on disk.

Each named-config DTS/DTSMON run also writes a fresh version-local
`config/DTSMON.json` containing only current DTSMON tests that satisfy the allowed
patterns, preserving configured sensor ordering and appending new named sensors in
first-seen order across all input rows, as in the reference sensor-update method.
The original DTSMON seed is retained as `config/DTSMON.seed.json`;
shared configuration files are not overwritten. Legacy configurations without
a named `DTS.json` keep their existing category rules.

DTSMON adapts `DTS-plotting/DTSDataBOT_rev7.py`'s `calculate_dtsmax` method:
the highest finite sensor reading per unit and full test name, keeping the first
input occurrence on ties. In CLASS mode this calculation is performed independently
inside each socket: Unit_ID + full Test + Socket. Socket is retained in summaries,
downloads, chart labels and hover data. In SORT mode the key remains Unit_ID + full
Test. Lot is taken from the winning row, not used as a maximum grouping key.
Variability charts show DTSMax by test, grouped by test namespace,
with winning sensor/unit/lot hover details and fixed 90/120 reference thresholds.
Full-data summaries and downloadable winners are unsampled; chart sampling does
not affect maxima. This is browser-native Plotly, not a JMP launch.

For AQUA reports, the selected analysis domain controls the parameter file:
CLASS uses `config/NVL_Aqua_Class_Automation_Parameters.txt`, while SORT uses
`config/NVL_Aqua_Sort_Automation_Parameters.txt`. Both use `-ReportConfig` with a
local report copy. CLASS copies are restricted to configured sensor tests. SDX
copies preserve the complete supplied export because reducing its parameter groups
can make AQUA reject the report; only configured tests are imported for analysis.
The supplied SORT export declares
`PreAssembly` / `SDX`; that source domain is preserved and validated independently
of the report's `SORT` label. Mixed source domains and mismatched operations are
rejected. Original parameter files remain unchanged and are frozen with hashes
in each successful retrieval's provenance. Select the domain and **Pull + process**
to retrieve fresh data; changing the selection alone does not retrieve data.
The SORT diode configuration includes the exact CPU0/CPU1 `_ERTD_DELTA` test names
returned by AQUA. They remain separate from the non-delta tests and do not inherit
their limits.
For normal report processing, place exports in `input/profiles/<slug>/`.
Lot/program filters must match the imported metadata exactly. Product choices
come from `config/Product_Name.txt`, grouped under `@ CLASS` and `@ SORT` with
numbered entries. Edit that file to maintain the list, then click **Refresh**
and reopen the report form. API saves reject products outside the domain list.
The selected product labels the report's `Product_Name` column and plot headings;
the source product remains unchanged in the cache and report metadata. Configured
product choices are report assignments, not exact raw-product filters. Legacy
workspaces without this list retain their existing exact-product filtering.

Saving a product-only edit generates a new diode report when the original local
source files are available and unchanged. Previous versions remain intact.
If sources changed or are unavailable, the report is saved with a warning to
process again; old reports are not silently relabeled. Query/domain changes
also require processing again.

The **Recipe Revision** table below the full-data product/test statistics shows
`PCS_STIM_RECIPE_VERSION` for CLASS and `RECIPEMANIFEST` for SORT, grouped by
product, lot, program, field, and revision with distinct-unit counts. Recipe
values are retained as text, separately from numeric sensor readings, including
leading zeros and complete manifest values. Only metadata belonging to units in
the current report selection is shown. A snapshot without recipe metadata shows
an empty-state message; use **Pull + process** to retrieve these fields and create
a new report. Previously saved reports are not modified.

New diode reports default to a sampled scatter plot per product/test. Each plot's
**Chart type** selector offers Histogram, Scatter, Line chart, and Distribution.
Distribution combines the exact histogram, a quartile box plot with 1.5-IQR
whiskers, and full-data moments, quantiles, confidence interval, and limit counts.
Scatter and line use deterministic samples up to the plot limit (5,000 by default),
ordered by source, lot, unit, sensor, and import row, not acquisition time.
Their titles show plotted and total counts; distribution outlier dots use the
same sample, while its outlier count and statistics use all readings. Chart
choices work in both the embedded report and the standalone HTML.

Thermal Diode test/sensor selection uses `config/Thermal_Diode.json`; DTS uses
`config/DTS.json`. DTS HTML includes a **Sigma multiplier** control (default 3,
range 0-10) for calculated limits: mean +/- multiplier x sample standard
deviation. Sigma statistics pool all sensor readings within each product/test/socket,
independent of plot sampling; every sensor in that group shares the same calculated
limits. Per-sensor summaries remain available, with `Test_count`, `Test_mean` and
`Test_stddev_samp` identifying the pooled basis. Calculated limits update both
chart views and the summary table; **Download calculated limits** exports the current
values. A test/socket with fewer than two readings has undefined sample SD and N/A
calculated limits. These are exploratory limits,
separate from configured specification limits; changes last for the HTML session
and do not change JSON configuration or saved PowerPoint exports.
DTS point tooltips in both chart views also show the imported socket, thermal
head, tester and tester site alongside the unit and lot.

Thermal Diode scatter/line points retain unit, sensor, lot, socket, tester,
thermal-head and tester-site identity. Clicking a point opens **Tester /
thermal-head backtrace** with matching equipment filters. Its statistics use the
full selected data, and its CSV download includes every reading and source path,
not just plotted samples. Equipment aliases such as `TesterId`, `Thermal_Head_Id`
and `Unit_Tester_Site_Id` are recognized. Missing IDs remain unavailable. TRACE
uses a job-level tester only when exactly one tester is known; ambiguous tester
lists are not assigned to individual units. Reprocess existing reports to add
these controls, and share the whole report folder to retain its CSV download.

All category-report tables start collapsed, including statistics, recipe,
equipment and DTSMON verification tables. Select a table heading to expand it;
calculations and downloads still include its data while collapsed. Socket plots
remain expanded by default. Reprocess older reports to adopt these defaults.

Reports also contain one combined summary table with count, units, mean, sample SD, min/max, LCL/UCL and counts
outside the limits. Values equal to a limit are within specification. Missing
limits are shown as N/A. Limits use the longest case-insensitive test-name match
in `config/Product_Test_Limits.json`. Its `_product_aliases` maps `nvlHX` and
`HX28C` to `NVL_Class_HX28C` (case-insensitive). The report's limit product is
recorded in each snapshot separately from the source product; this does not
relabel imported readings. The four configured CLASS
diode tests currently use LCL 95 and UCL 105. Existing snapshots stay unchanged;
process again to generate a new report version after changing limits.

Generate the offline HTML library and its existing report files:

```powershell
python run_dashboard.py --publish-profiles --service-url http://127.0.0.1:8063
```

Open `output/shared/index.html`. Saved plots work offline; **Open live workspace**
opens the running Python service for add/copy/edit/delete operations. Share the
whole `output/shared` folder to retain its report links. Publishing does not pull
new AQUA data or generate readings for empty reports. Report labels are not
authentication; keep the service on loopback or behind an authenticated proxy.

New Thermal Diode HTML reports include **Enabled tests** checkboxes and an
**All tests** checkbox. Disabling a test hides its plots and product/test
statistics rows; enabling it restores them without reprocessing. These controls
work in the portal's embedded report and in standalone HTML. All tests start
enabled, and selections reset when the report is reloaded. This is a view-only
selection: source data, limits, report totals, recipe metadata and PowerPoint
exports are unchanged. Regenerate older reports to add the controls.

CLASS Thermal Diode reports have a separate plot set for every socket present in
the source data, including CLASSHOT, CSM, PHMHOT and other values. Each set has
histogram, scatter, line and distribution views, with statistics and limit checks
calculated independently per product, test and socket. The full-data statistics
and recipe tables identify the socket; PowerPoint chart titles also include it.
Test checkboxes apply to that test across all sockets. SORT reports retain their
combined layout.

Socket metadata comes from `Socket` (also `Socket Name` / `socket_name`), falling
back to `ProcessStep` (also `Process Step` / `process_step`), then `SubFlowStep`
(also `SUBFLOW_STEP`, `Subflow Step` / `Sub Flow Step`). Latest-operation flags
are not socket labels. Compact aliases such as `SocketName` and `SocketId` are
also recognized. Placeholder values such as N/A, null and Unknown socket are
skipped so valid fallback metadata can be used. Known labels normalize case and
separators (for example, `phm room` becomes `PHMROOM`); other explicit values are
preserved. AQUA exports must include an actual socket/process-step
column or an operation-prefixed `OPERGROUP`, such as `6248_CLASSHOT`. The
`OPERGROUP` suffix is the final fallback after Socket, ProcessStep and SubFlowStep;
an operation number alone does not identify a socket. This applies equally to
downloaded CSV and imported Excel workbooks. Selecting a subflow in the report
query does not supply socket metadata. Missing metadata is
shown as **Unknown socket**, not inferred from the report's query label. This
applies to AQUA, TRACE and manual exports that provide these columns. Reprocess
existing CLASS reports to ingest socket metadata and generate the new layout;
publishing alone does not regenerate old plots. Cache migration retains existing
rows and reimports available source files on the next processing run.

Click a socket heading or focus it and press Enter/Space to minimize or restore
that socket's plots. All sockets start expanded. Collapsing is view-only: it
keeps statistics and exports unchanged, and test filtering preserves each
socket's expanded/collapsed state until reload. Reprocess older reports to add
these controls.

### Socket-specific limits

In `config/Product_Test_Limits.json`, each test can include a `sockets` object:

```json
{
	"test": "PCS_SOT0",
	"sockets": {
		"CLASSHOT": { "UCL": 107, "LCL": 93 },
		"CSM": { "UCL": null, "LCL": null },
		"PHMHOT": { "UCL": null, "LCL": null }
	}
}
```

Replace the null placeholders with the approved limits for each socket; null
means not configured, not zero. Add other socket names as additional keys.
The four PCS_SOT entries under `NVL_Class_HX28C` now reserve the supplied limits
for CLASSHOT. Other existing tests and SORT limits are unchanged.

The longest matching test name is selected first. Socket names then match
exactly, ignoring case and surrounding spaces. A matching socket replaces the
entire LCL/UCL pair; missing or null bounds are N/A and do not inherit a default.
Optional top-level `UCL` and `LCL` values serve as defaults only when no socket
matches (including missing socket metadata). Without those defaults, unlisted
sockets show N/A. Socket-aware CLASS plots, limit counts and PowerPoint exports
use the same resolved pair. Reprocess the report after editing limits; previously
published snapshots retain their frozen values.

## PowerPoint Template

PowerPoint exports use `config/PPT_Template.pptx` when present. Its first slide
provides the cover layout and its second slide provides the content layout.
Sample slides are replaced with the report cover, combined plots, and one data
summary table. The original template is not modified.

Template exports require installed Microsoft PowerPoint on Windows and `pywin32`
in the server's Python environment. The signed-in Office user must have access
to the template. Rights-management protection is retained in downloaded decks;
it can restrict sharing and image export. Without a template, the built-in
PowerPoint format remains available.
PowerPoint uses the saved default histogram figures; interactive chart choices
in the HTML report do not change the cached PowerPoint.

## Folders

- `Input_data`: input CSV, tab-delimited TXT and XLSX files (first worksheet).
- `Config`: sensor-name JSON configuration and Product_Test_Limits.json.
- `Saved_Sessions`: generated DuckDB database and temporary spill directory.
- `HTML_Output`: generated PDF and PNG reports.
- `Profiles`: saved report definitions retained with the distribution.
- `_internal`: retained legacy runtime files, not required for HTML viewing.

The maintainer workspace retains source scripts, tests, input and configuration.
The shared HTML publication contains reports and exports, not a Python runtime.

## Lot Comparison

Open **Test Analysis > Lot Comparison**. Select Product, BOM, PKG, Test Name
and optional Sensor. Use the existing Lot multi-select in Advanced Filters to
select lots; leave it empty for all lots. Program, VISUAL_ID, Test Name and
global Data Filter selections also apply. Filters update immediately.

Each table row represents a separate Product/BOM/PKG/test/sensor/lot group.
Count, mean, population standard deviation, minimum and maximum use every
matching valid reading. The chart displays up to 50 groups on the current table
page with mean markers and standard-deviation bars. Page through all groups.
Dropdown results are limited to 200 matches; type to search beyond them.

The input requires `TEST_NAME`/`test_name` and `TEST_RESULT`/`test_result`.
Multiple readings are pipe-delimited. `Lot`/`LOTS`, `BOM`, `PKG`/`Package`,
`PRODUCT`/`Product_Name`, `VISUAL_ID` and `DevRevStep` are supported.
Explicit product metadata takes priority over the existing DevRevStep mapping.
Missing BOM/PKG are displayed as missing, never inferred. The supplied workbook
contains Package but no BOM; add a BOM column to compare that dimension.
Text lot and unit IDs preserve leading zeros; Excel numeric formatting is not
an identifier value, so store such identifiers as text in Excel.

## Large Data

Data is streamed into DuckDB in batches of 5,000 expanded readings by default,
using one worker thread to limit peak memory. Batches are committed incrementally
instead of holding a full-file transaction; interrupted imports are cleaned up
and retried on the next refresh. Full-data
statistics, counts and failure analysis run in SQL. Product scatter plots and
PDF/PNG exports use up to 5,000 sampled readings per test; a Product page has a
200,000-point total budget. Counts and control limits are not sampled.
Dashboard box summaries use full-data quartiles. Bin commonality displays the
top 200 categories, and repetition shows the top 200 units with full totals.
Large numbers of tests still produce many graphs when Show All is selected;
use the existing First 5/20 setting or test filters on constrained browsers.

`python run_dashboard.py --memory-limit 1GB --batch-size 5000 --threads 1`

The memory limit is a DuckDB buffer budget, not a total process-memory cap.
Excel shared strings, aggregate state and Python/native libraries use additional
memory. CSV is preferred for very large acquisitions. Convert legacy XLS files
to XLSX or CSV. Formula cells require saved calculated values.

Allow disk space for the database, transaction changes, spill files and exports.
Use `--database D:\ThermalCache\thermal.duckdb` to place storage on another
drive. Refresh Data detects added, changed and removed input files and config
changes. Clear Cache invalidates the import fingerprints without deleting input.
Malformed input aborts and rolls back the import; errors appear in the console.

PNG/PDF rendering uses Kaleido and needs an installed supported Chrome/Chromium
browser. A missing renderer/browser produces an export error, not a data loss.
Bootstrap styling currently uses the same online CDN as the original dashboard.

## Source and Rebuild

For source execution, create a virtual environment, install
`requirements_dashboard.txt`, then run `python run_dashboard.py`.
The default interface uses disk-backed queries; `--large-data` is only an
optional compact view, and `--legacy` is an alias for the original interface.

Run `./build_dashboard.ps1` from PowerShell to test and generate the HTML library,
then `./publish_dashboard.ps1` to publish it to the configured network share.
See PACKAGE_README.md for the approved test-exception option and deployment details.
Previously generated dashboard executables have been retired.