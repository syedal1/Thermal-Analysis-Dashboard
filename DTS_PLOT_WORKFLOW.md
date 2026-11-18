# DTS Processing and JMP Plot Workflow

## MCP Servers and AQUA CLI

### TRACE MCP (required for metadata preflight)

Connect TRACE MCP before running the README's job and socket metadata steps:

| Preflight step | Tools | Purpose |
|---|---|---|
| 1 | `find_jobs`, `get_bin_pareto` | Look up jobs for the requested lot and the unit's interface bin. |
| 2 | `get_detailed_jobs` | Obtain per-socket tester name, temperature, and process step. |

Without TRACE MCP connected, these two preflight steps cannot run. No other MCP
server is required. These are metadata preflight steps, not steps 1-2 of the
report-processing table below.

**Implementation boundary:** `WW43p1_DTSDataBOT_rev7.py` does not currently call
TRACE MCP or automate this preflight. Its existing report-download path and
local-file processing can run independently; they do not establish that TRACE
metadata was checked.

### AQUA CLI Client (not an MCP server)

No local AQUA installation is needed. Invoke the executable directly from its
shared network path using PowerShell:

```text
\\PGSAPP3301.gar.corp.intel.com\Installer\AquaHbase\AquaCMDClient\Client\AquaCmdLine.exe
```

Corporate network access and permission to execute the shared client are required.
For the README's metadata pipeline, use `UNITS_LIST` analysis to retrieve
per-socket `LOTS_End_Date_Time`. For example, for the requested CLASSHOT operation:

```powershell
& '\\PGSAPP3301.gar.corp.intel.com\Installer\AquaHbase\AquaCMDClient\Client\AquaCmdLine.exe' -aquaServer GAR -analysisType UNITS_LIST -lots P642374MV -operations 6248 -outputFileName "$PWD\output\aqua_units_P642374MV_6248_$(Get-Date -Format yyyyMMdd_HHmmss).csv"
```

Run this from the project root with an existing `output` directory. Check the CLI
exit code, returned lot/operation, socket identifiers, and timestamps before using
the export. Row count is not necessarily the number of unique units.

`UNITS_LIST` uses direct CLI arguments; it does not require staging a report in
the AQUA cache or setting `AQUA_REPORT_PATH`. It supplies metadata, not DTS-MON
sensor results. It cannot replace the sensor CSV containing `VISUAL_ID`,
`TEST_NAME`, and `TEST_RESULT` required for DTSMax calculation and plotting.
The DTS-MON report retrieval below is a separate CLI operation with its own
report-path/cache requirements. Neither operation uses an AQUA MCP tool.

## Prompt-driven run: lot to DTS-MON plot

With an accessible DTS-MON report configured, one prompt retrieves a lot from
AQUA, processes it, and plots the DTS-MON distribution:

```text
Plot DTS-MON for P642374MV CLASSHOT
```

Format: `Plot DTS-MON for <Lot> <SubFlowStep>`. The sub-flow step must match the
`SubFlow Step` already defined in `config/AQUA_LOT_DATA_ENTRY.txt` (`CLASSHOT` in the
`6248|CLASSHOT` operation).

| Step | Action | File or tool | Result |
|---|---|---|---|
| 1 | Read the lot and sub-flow step from the prompt | prompt | `P642374MV`, `CLASSHOT` |
| 2 | Replace the lot value under `@@ Lot :` | `config/AQUA_LOT_DATA_ENTRY.txt` | AQUA report filtered to the requested lot |
| 3 | Run the AQUA command-line client and download the report | `AquaCmdLine.exe` | Raw CSV in `input` |
| 4 | Process all units into DTSMax, per unit, full test name, and lot | `DTSDataBOT_rev7.py` | JMP table and threshold summaries in `output` |
| 5 | Plot the DTS-MON distribution | `config/Script Window.jsl` in JMP | `Report.html`, `preview.png`, `status.txt` |

**Step 2 rules**

- Only the value on the line after `@@ Lot :` changes. `SubstructureId`, the operation,
  and all parameter groups stay as they are.
- Accept one lot made of letters and digits only (for example `P642374MV`); reject anything
  else before writing the file.
- Stop if the prompt step does not match the entry's `SubFlow Step`. The file is only
  rewritten when the lot and step both pass, and the write is atomic.

**Steps 3 to 5** are described in [AQUA Retrieval and Processing](#aqua-retrieval-and-processing).
Any failure stops the run: a failed download never falls back to an older CSV or summary.

**Outputs**

- Raw AQUA CSV: `input/<report-name>_<timestamp>_<id>.csv`.
- JMP table: `output/<report-name>_<timestamp>_<id>_JMP_DTSMax.txt`.
- Summaries: the `_DTSMax_summary.csv` and `_Over_test.csv` / `_Under_test.csv` files beside the JMP table.
- Plot: `output/<report-name>_<timestamp>_<id>_Script_Window/Report.html` and `preview.png`.
- Combined report: `output/<stem>_Full_Report.html`, a single self-contained HTML holding every
  result except the full sensor table. See [Combined HTML Report](#combined-html-report).
- File names come from the report name, not the lot. Use the `Lot` column to identify the lot in the data.

Run the same request from a command prompt; the script parses it and does steps 1 to 5:

```powershell
& 'C:\Program Files\Python314\python.exe' -X utf8 .\WW43p1_DTSDataBOT_rev7.py Plot DTS-MON for P642374MV CLASSHOT
```

`--lot P642374MV --step CLASSHOT` is the equivalent explicit form. `--step` is optional, and
neither form can be combined with `--existing-csv`. The script prints
`AQUA entry Lot: <old> -> <new>` after updating the file.

**Saved report on the AQUA server (no cache folder needed).** If you cannot write to the
server cache, save the DTS-MON report in AQUA and pass its server path. The script then runs
`AquaCmdLine.exe -reportPath <path> -lots <Lot>` and writes the CSV locally. The local entry
file is not edited and `SubFlow Step` is not checked, because the saved report owns its filters.

```powershell
& 'C:\Program Files\Python314\python.exe' -X utf8 .\WW43p1_DTSDataBOT_rev7.py --report-path '<server path of saved report>' Plot DTS-MON for P642374MV CLASSHOT
```

Set `AQUA_REPORT_PATH` to make that path the default.

## Combined HTML Report

Every run ends with one self-contained HTML report, `output/<stem>_Full_Report.html`,
where `<stem>` is the input file name without extension (for example
`P644225CR_P642609MV_CLASSHOT_20261103_DTSMON`). The JMP chart images are embedded
as base64, so the file can be shared or archived on its own without the `gfx` folder.

Sections, in order:

| Section | Content | Source |
|---|---|---|
| Header | Lots, operation, DTSMax rule, 90/120 C reference limits and setpoint caveat | run metadata |
| Per-lot summary | Source units, units with data, DTSMax observations, over/under/within counts, min/max | `_output_DTSMax_by_lot.csv` |
| Per-domain summary | The same counts by `Test_Domain` and `Lot` | `_JMP_DTSMax.txt` |
| JMP charts | One variability chart per domain, titled as in JMP | `_Script_Window/Report.html` + `gfx/*.png` |
| Affected-test summary | Per test: count, over/under counts and percentages, min/max | `_output_DTSMax_summary.csv` |
| Over-test detail | Tests with DTSMax > 120 C and hottest sensor | `_output_DTSMax_Over_test.csv` |
| Under-test detail | Tests with DTSMax < 90 C and hottest sensor | `_output_DTSMax_Under_test.csv` |
| Excluded units | Units with no TEST_NAME/TEST_RESULT, with functional/interface bin and reason | input CSV |
| Verification | Source rows/keys, full sensor rows, recomputed maxima, plotted rows, missing keys, value mismatches, duplicates, non-finite values, 4000-char truncation | `_verification.json` |
| Other files | Full sensor TSV (name, size, rows), JMP table, JMP report, verification JSON | `output` |

The full sensor table (`_output.txt` / `.txt.gz`) is listed but not embedded; at millions
of rows and over 1 GB it cannot be opened in a browser.

Generate it after processing and `--summarize-dtsmax` have completed. Both scripts live in
the project root and take the input stem as their only argument, so nothing has to be
copied into `output`:

```powershell
$s = '<stem>'
& 'C:\Program Files\Python314\python.exe' -X utf8 DTSDataBOT_rev7.py --summarize-dtsmax "output/${s}_output.txt"
& 'C:\Program Files\Python314\python.exe' -X utf8 .\verify_dts_output.py $s
& 'C:\Program Files\Python314\python.exe' -X utf8 .\make_dts_html.py $s; Start-Process ".\output\${s}_Full_Report.html"
```

- `verify_dts_output.py` recomputes maxima from the full export in chunks, compares them
  with the JMP table and source keys, and writes `<stem>_verification.json` and
  `<stem>_output_DTSMax_by_lot.csv`.
- `make_dts_html.py` reads those files plus the summary CSVs, JMP report and input CSV, and
  writes `<stem>_Full_Report.html`. It prints the output path, size and number of embedded
  charts; the chart count should equal the number of domains in the JMP report.

The input CSV must still be in `input` because both scripts read it.

## DTSMax Grouping

For DTSMON inputs, DTSMax is the highest numeric sensor reading for each
`VISUAL_ID`, full `TEST_NAME`, and `Lot` combination. The same unit/test in
different lots has a separate maximum in each lot. Repeated readings within
the same unit/test/lot are combined; ties retain the first winning sensor row.
Only that row receives DTSMax; all other sensor rows remain in the full export.
Missing lot values form a separate group. Without a Lot column, grouping falls
back to unit/test. Missing unit/test keys and nonnumeric readings are excluded.
JMP and threshold summaries use the finite DTSMax winners, not every sensor row.

## AQUA Retrieval and Processing

Run the new entry point with the installed Python interpreter:

```powershell
& 'C:\Program Files\Python314\python.exe' -X utf8 .\WW43p1_DTSDataBOT_rev7.py
```

This reads `config/AQUA_LOT_DATA_ENTRY.txt` as it is (no Lot argument leaves it untouched) and uses the AQUA CLI at
`\\PGSAPP3301.gar.corp.intel.com\Installer\AquaHbase\AquaCMDClient\Client\AquaCmdLine.exe`.
Corporate network access and permission to run AQUA are required. The CLI resolves
report paths on the server, so the script first copies the report to
`\\PGSAPP3301.gar.corp.intel.com\AQUA_HBASE\Cache\<USERNAME>`.
That folder must be writable. Set `--aqua-cache-dir` to an authorized, writable
server-accessible report folder when the default is unavailable. For example:

```powershell
& 'C:\Program Files\Python314\python.exe' -X utf8 .\WW43p1_DTSDataBOT_rev7.py --aqua-cache-dir '\\server\share\your-report-folder'
```

Replace the example folder with a real accessible location. Use `--aqua-cli` (or
`AQUA_CLI`) to override the executable, and `--aqua-server GAR|AMR|GER` to choose
the region. No passwords, tokens, or elevated privileges are requested.

After the Lot is set in the entry file, the script:

1. Stages a uniquely named report copy on the server without changing its filters.
2. Retrieves CSV into a temporary directory under `input`, validates required
	columns and a data row, then publishes a uniquely named CSV directly in `input`.
3. Reuses `DTSDataBOT_rev7.py` to discover sensor mappings and process all units
	into a finite-DTSMax table without sampling. The downloaded raw CSV is retained.
4. Saves affected-test summaries and over-test/under-test CSVs under `output`.
5. Executes `config/Script Window.jsl` in JMP and checks its success status plus
	the generated `Report.html` and `preview.png`.

Over-test means DTSMax >120 C; under-test means DTSMax <90 C. Values at 90 and
120 C are within range. These fixed reference thresholds are not adjusted for
test setpoints, so low-temperature tests can be flagged intentionally.

Use `--full-sensors` to also retain every processed sensor row as `.txt.gz`;
otherwise only the raw CSV and processed maxima are saved. Use `--sensor-config`
for positional sensor data; named sensor data can generate its own configuration.
`--skip-jmp` performs retrieval, processing and summaries without opening JMP.
`--timeout` sets the AQUA/JMP subprocess timeout in seconds (default 3600).

To explicitly reprocess a previously downloaded CSV without querying AQUA:

```powershell
& 'C:\Program Files\Python314\python.exe' -X utf8 .\WW43p1_DTSDataBOT_rev7.py --existing-csv 'input\<downloaded-DTSMON-report>.csv'
```

The AQUA workflow passes explicit paths and permits multiple input files. Failed
retrieval never falls back to an older input or summary. Server-side report copies
are retained for traceability; existing local files are not deleted.

## Plug-and-play run

1. Put exactly one `.xlsx`, `.xls`, `.csv`, or `.txt` input file in `input`.
2. Double-click `Run_DTS_Plot.bat`, or run:

```powershell
.\Run_DTS_Plot.ps1
```

The runner automatically:

- executes `DTSDataBOT_rev7.py --streaming` in automatic folder mode;
- finds the generated `<input-name>_output.txt` file;
- streams finite DTSMax observations into `<input-name>_JMP_DTSMax.txt` for JMP;
- includes `config/Script Window.jsl`;
- preserves the script's dynamic axis range and 90/120 reference lines;
- saves `Report.html`, `preview.png`, and `status.txt` in `output/<input-name>_Script_Window`.

Open the generated `Report.html` to view the plot. The report folder contains its chart images, so the HTML remains usable when shared.

## Large Files

For limited disk space, generate only the complete DTSMax plot table:

```powershell
.\Run_DTS_Plot.ps1 -PlotOnly
```

This processes all input data, retaining every finite unit/test maximum without sampling.
It skips the large all-sensor output. The JSL uses the same plotted observations and grouping columns.
The compact output also retains `Unit Tester Id` and `Unit Tester Site Id` when available,
using the metadata from each winning DTSMax sensor row.
To relaunch an already generated compact table, use `-PlotOnly -SkipProcessing`.

### Lossless All-Sensor Compression

To retain every sensor row with less disk usage, specify a `.txt.gz` streaming output.
Use explicit input and config paths (automatic folder mode chooses its own output name):

```powershell
python -X utf8 DTSDataBOT_rev7.py --streaming -i input/<input-name>.xlsx -c config/<input-name>_config.json -o output/<input-name>_output.txt.gz
python -X utf8 DTSDataBOT_rev7.py --prepare-jmp-data output/<input-name>_output.txt.gz -o output/<input-name>_JMP_DTSMax.txt
python -X utf8 DTSDataBOT_rev7.py --summarize-dtsmax output/<input-name>_output.txt.gz
.\Run_DTS_Plot.ps1 -PlotOnly -SkipProcessing
```

Replace `<input-name>` with the workbook stem. Gzip preserves all columns and sensor
rows; only storage encoding changes. JMP preparation and summaries read the archive
directly, without extracting a full TSV. JMP still plots finite DTSMax observations.
Compression ratio depends on the data; temporary staging and the disk-space guard
still apply. Decompressing the full archive later requires space for the full TSV.

Streaming supports XLSX (first worksheet), CSV, and tab-delimited TXT. Legacy XLS files
must first be converted to XLSX or CSV. XLSX is read in read-only mode; Excel shared
strings still occupy memory. A temporary SQLite database under `output` groups all
rows for each unit, including interleaved rows, before processing. RAM usage depends
on the largest complete unit and the sensor configuration, not just the chunk size;
`--memory-limit` is advisory, not a hard RAM cap. Output order is grouped by unit.

Temporary staging and output files need free disk space. Processing stops when less
than 512 MiB remains at a batch boundary, cleans up staging, and leaves any previous
final output intact. Full sensor output can be many times larger than the input.
JMP opens the nine plot columns plus available tester ID/site columns and rows with finite DTSMax values; very large
numbers of test instances can still make chart rendering slow.

## All-Sensor Plots

To plot every sensor reading (`Value`) instead of only `DTSMax`, run:

```powershell
.\Run_DTS_Plot.ps1 -AllSensors
```

For an existing full, uncompressed TSV on another drive:

```powershell
.\Run_DTS_Plot.ps1 -AllSensors -SkipProcessing -DataFile K:\DTS_Output\WW42p3_U8C_DTSMON_output.txt
```

The all-sensor report is saved separately under `output/<input-name>_All_Sensors_Script_Window`.
It imports every sensor row, retains sensor IDs and metadata, and plots the `Value`
column with the existing grouping and reference lines. The runner generates a copy
of `config/Script Window.jsl` with `DTSMax` replaced by `Value`; the original JSL
and default DTSMax workflow remain unchanged. Missing numeric readings cannot be
plotted, but remain in the full table. `AllSensors` cannot be combined with `PlotOnly`.

## Input rule

Keep only one supported input file in `input`. Excel lock files beginning with `~$` are ignored.