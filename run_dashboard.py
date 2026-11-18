#!/usr/bin/env python3
"""
Thermal Dashboard Launcher
Run the Thermal Analysis Dashboard application

Usage:
    python run_dashboard.py              # Original dashboard interface
    python run_dashboard.py --large-data # Optional compact analysis view
    python run_dashboard.py --no-browser # Run without opening browser
    python run_dashboard.py --debug      # Debug mode
    python run_dashboard.py --port 8000  # Custom port
"""

import sys
import os
from pathlib import Path

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Launch Thermal Analysis Dashboard')
    parser.add_argument('--desktop', action='store_true', help='Open the approved socket baseline in a private local workspace (packaged default)')
    parser.add_argument('--release-root', type=Path, help='Published release folder containing release.json and baseline')
    parser.add_argument('--workspace-root', type=Path, help='Private desktop storage (default: LOCALAPPDATA/ThermalAnalysisDashboard)')
    parser.add_argument('--stage-trace-fallback', metavar='REPORT_SLUG', help='Stage normalized TRACE MCP exports for this report')
    parser.add_argument('--trace-files', nargs='+', type=Path, help='Normalized TRACE CSV/TSV/TXT files to stage')
    parser.add_argument('--trace-retrieved-at', help='Timezone-aware ISO-8601 TRACE retrieval time')
    parser.add_argument('--no-browser', action='store_true', help='Do not open browser')
    parser.add_argument('--export-html', action='store_true', help='Write output/dashboard.html and exit')
    parser.add_argument('--product', help='Export only this exact product name with --export-html')
    parser.add_argument('--cached', action='store_true', help='Export existing cache without refreshing; label source freshness')
    parser.add_argument('--plot-limit', type=int, default=5000, help='Maximum sampled points per exported test (1-20000)')
    parser.add_argument('--debug', action='store_true', help='Run in debug mode')
    parser.add_argument('--port', type=int, default=8050, help='Port to run on (default: 8050)')
    parser.add_argument('--host', default='127.0.0.1', help='Host to bind to (default: 127.0.0.1)')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--large-data', dest='large_data', action='store_true', help='Use optional compact analysis view')
    mode.add_argument('--legacy', dest='large_data', action='store_false', help='Use original dashboard interface (default)')
    parser.set_defaults(large_data=False)
    parser.add_argument('--memory-limit', default='1GB', help='DuckDB memory budget (default: 1GB)')
    parser.add_argument('--batch-size', type=int, default=5000, help='Maximum readings per import batch (default: 5000)')
    parser.add_argument('--threads', type=int, default=1, help='DuckDB worker threads (default: 1)')
    parser.add_argument('--database', type=Path, help='Large-data database path; spill files use the same drive')
    parser.add_argument('--export-folder', type=Path, help='Export folder (default: output)')
    portal_mode = parser.add_mutually_exclusive_group()
    portal_mode.add_argument('--profile-portal', action='store_true', help='Serve the multi-user report HTML workspace')
    portal_mode.add_argument('--publish-profiles', action='store_true', help='Publish the shared report HTML library and exit')
    parser.add_argument('--publish-folder', type=Path, help='Shared portal destination (default: output/shared)')
    parser.add_argument('--service-url', help='Approved HTTP(S) service URL linked from the static portal')
    
    args = parser.parse_args()
    if args.batch_size < 1 or args.threads < 1:
        parser.error('--batch-size and --threads must be positive')
    if not 1 <= args.plot_limit <= 20000:
        parser.error('--plot-limit must be between 1 and 20000')
    if args.cached and not args.export_html:
        parser.error('--cached requires --export-html')
    application_root = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent
    if args.stage_trace_fallback:
        if not args.trace_files or not args.trace_retrieved_at or args.export_html or args.large_data \
                or args.profile_portal or args.publish_profiles:
            parser.error('--stage-trace-fallback requires --trace-files and --trace-retrieved-at, '
                         'and cannot be combined with another dashboard mode')
        release_root = args.release_root or (
            Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else application_root)
        if getattr(sys, 'frozen', False):
            from desktop_release import prepare_workspace

            workspace, _ = prepare_workspace(release_root, args.workspace_root)
        else:
            workspace = (args.workspace_root or release_root).resolve()
        from stage_trace_fallback import stage

        print(f'TRACE fallback staged: {stage(workspace, args.stage_trace_fallback, args.trace_files, args.trace_retrieved_at)}')
        return
    elif args.trace_files or args.trace_retrieved_at:
        parser.error('--trace-files and --trace-retrieved-at require --stage-trace-fallback')
    explicit_mode = any(option in sys.argv[1:] for option in (
        '--legacy', '--large-data', '--export-html', '--profile-portal', '--publish-profiles'))
    if args.desktop or (getattr(sys, 'frozen', False) and not explicit_mode):
        if args.desktop and explicit_mode:
            parser.error('--desktop cannot be combined with other dashboard modes')
        from desktop_release import launch_desktop

        release_root = Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else application_root
        launch_desktop(args.release_root or release_root,
                       port=args.port if '--port' in sys.argv[1:] or any(value.startswith('--port=') for value in sys.argv[1:]) else 8063,
                       no_browser=args.no_browser, local_root=args.workspace_root)
        return
    os.chdir(application_root)

    if args.profile_portal or args.publish_profiles:
        if args.export_html or args.large_data:
            parser.error('Report portal options cannot be combined with --export-html or --large-data')
        from shared_dashboard import create_app, publish_portal

        root = Path.cwd()
        destination = args.publish_folder or root / 'output' / 'shared'
        if args.publish_profiles:
            print(f'Shared HTML: {publish_portal(root, destination, args.service_url)}')
        else:
            if args.host not in ('127.0.0.1', 'localhost', '::1'):
                parser.error('The unauthenticated portal must bind to loopback; use an authenticated reverse proxy for shared access')
            portal = create_app(root, destination, args.service_url)
            print(f'Report workspace: http://{args.host}:{args.port}')
            print(f'Published HTML folder: {destination}')
            print('Owner names are labels, not authenticated identities. AQUA/TRACE requires a configured connector.')
            portal.run(host=args.host, port=args.port, debug=False, use_reloader=False)
        return
    
    # Import the dashboard app
    from atlas_thermal_dashboard_optimized import app, OUTPUT_FOLDER, CONFIG_FOLDER, DATA_FOLDER, PERSISTENT_DATA_FOLDER
    
    # Create required folders
    for folder in [OUTPUT_FOLDER, CONFIG_FOLDER, DATA_FOLDER, PERSISTENT_DATA_FOLDER]:
        os.makedirs(folder, exist_ok=True)

    if args.export_html:
        from atlas_thermal_dashboard_optimized import PRODUCT_MAPPING, load_config_from_folder
        from large_data_store import ThermalStore
        from export_dashboard_html import export_dashboard

        database = args.database or Path(PERSISTENT_DATA_FOLDER) / 'thermal_large.duckdb'
        if args.cached and not database.is_file():
            parser.error('--cached requires an existing database')
        store = ThermalStore(database,
                             memory_limit=args.memory_limit, batch_size=args.batch_size, threads=args.threads)
        try:
            config = load_config_from_folder()
            if args.cached:
                current = store.is_current(DATA_FOLDER, config, PRODUCT_MAPPING)
                source_note = ('Source: existing cache; input/configuration fingerprints match.' if current else
                               'WARNING: cached data does not match current input/configuration. '
                               'This report may omit workbook changes or use older sensor mappings.')
                source_note += ' Specification limits use the current configuration.'
            else:
                store.refresh(DATA_FOLDER, config, PRODUCT_MAPPING)
                source_note = 'Source: refreshed from input files with current configuration.'
            print(source_note)
            destination = export_dashboard(store, args.export_folder or OUTPUT_FOLDER, product=args.product,
                                           source_note=source_note, plot_limit=args.plot_limit)
            print(f'HTML dashboard: {destination.resolve()}')
        finally:
            store.close()
        return

    if args.large_data:
        from atlas_thermal_dashboard_large import create_app
        from atlas_thermal_dashboard_optimized import PRODUCT_MAPPING, load_config_from_folder
        from large_data_store import ThermalStore

        store = ThermalStore(args.database or Path(PERSISTENT_DATA_FOLDER) / 'thermal_large.duckdb',
                             memory_limit=args.memory_limit, batch_size=args.batch_size, threads=args.threads)
        app = create_app(store, DATA_FOLDER, load_config_from_folder, PRODUCT_MAPPING, args.export_folder or OUTPUT_FOLDER)
        print(f"Large-data mode: DuckDB budget {args.memory_limit}, batch {args.batch_size:,}, threads {args.threads}")
    else:
        from atlas_thermal_dashboard_optimized import data_store

        data_store.database = args.database or Path(PERSISTENT_DATA_FOLDER) / 'thermal_large.duckdb'
        data_store.memory_limit = args.memory_limit
        data_store.batch_size = args.batch_size
        data_store.threads = args.threads
        print("Loading dashboard data...")
        data_store.load_data()
        print(data_store.load_status)
    
    # Print startup info
    print("\n" + "="*70)
    print("🌡️  Thermal Analysis Dashboard")
    print("="*70)
    print(f"\n📂 Configuration Folder: {CONFIG_FOLDER}")
    print(f"📂 Data Folder: {DATA_FOLDER}")
    print(f"💾 Persistent Data Folder: {PERSISTENT_DATA_FOLDER}")
    print(f"\n🚀 Starting dashboard server...")
    print(f"📱 Open browser to: http://{args.host}:{args.port}")
    
    if args.debug:
        print(f"🐛 Debug mode: ON")
    
    print("\n" + "="*70 + "\n")
    
    # Launch browser if requested
    if not args.no_browser:
        import threading
        import subprocess
        import webbrowser
        import shutil
        
        def _open_browser():
            import time
            time.sleep(2.0)  # Wait for server to start
            
            edge_paths = [
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            ]
            exe = next((p for p in edge_paths if os.path.exists(p)), None) or shutil.which("msedge")
            url = f"http://{args.host}:{args.port}"
            
            try:
                if exe:
                    subprocess.Popen([exe, url])
                    print(f"🌐 Opened dashboard in Microsoft Edge: {url}")
                else:
                    webbrowser.open(url)
                    print(f"🌐 Edge not found; opened default browser: {url}")
            except Exception as e:
                print(f"[launcher] Could not open browser: {e}")
                print(f"[launcher] Open manually: {url}")
        
        threading.Thread(target=_open_browser, daemon=True).start()
    
    # Run the app
    try:
        app.run(
            debug=args.debug,
            host=args.host,
            port=args.port,
            use_reloader=False
        )
    except KeyboardInterrupt:
        print("\n\n⚠️  Dashboard stopped by user")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Error running dashboard: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
