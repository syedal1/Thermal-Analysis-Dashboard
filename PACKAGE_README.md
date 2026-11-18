# Thermal Dashboard HTML Library

Open the shared folder and double-click `START_THERMAL_DASHBOARD.bat`:

    \\gar.corp.intel.com\ec\proj\mdl\pg\intel\engineering\dev\sctp\axl_user\cheekeon\Thermal-Analysis-Dashboard

This opens `index.html` in the default browser. It is a static, read-only HTML
library that works from the network share; no server or executable is required.
The approved report is selected automatically. Open the Reports tab to browse
other published snapshots. The launcher expects the browser to have access to
the network share.

The starting report is **2026-ww46p5_HX28C-copy-copy / PTH_THERMAL**, HX28C, lot
P636547CR. Its CLASSHOT, CSM, and PHMHOT socket plots contain 376 readings,
8 tests, and 24 plots. Socket sections can be collapsed, tests filtered, and
plots inspected. The static page is not the live VPO service shown in the
reference: it displays published reports and cannot look up arbitrary VPOs or
pull new data by itself.

## Updates

Only maintainers should have write access to the shared folder; ordinary users
need read access. Windows share permissions must enforce this separation.

1. Install the project requirements in a Python environment on the maintenance machine.
2. Run `./build_dashboard.ps1 -Python <python.exe>` to run tests and generate
   `output/shared/index.html` plus its report assets. The full suite still has
   six previously approved failures (legacy loader and AQUA integrations); use
   `-UseApprovedTestException` only for those known failures and investigate
   any new failures before publication.
3. Run `./publish_dashboard.ps1` to copy the static page and report assets to the
   shared folder. The index is switched atomically after its assets are copied.

The approved snapshot, source input, and prior reports are retained. The existing
`PPT` folder is not modified. To generate a standalone report locally, see
`EXPORT_HTML.bat` and the export instructions in [README.md](README.md).