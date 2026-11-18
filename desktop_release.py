"""Start a private report workspace from an administrator-published release."""

import json
import os
import shutil
import uuid
from pathlib import Path


def prepare_workspace(release_root, local_root=None):
    release_root = Path(release_root).resolve()
    release = json.loads((release_root / 'release.json').read_text(encoding='utf-8-sig'))
    from shared_dashboard import SLUG_PATTERN, VERSION_PATTERN

    for key in ('release_id', 'baseline_slug'):
        if not isinstance(release.get(key), str) or not SLUG_PATTERN.fullmatch(release[key]):
            raise ValueError(f'Invalid release {key}')
    if not VERSION_PATTERN.fullmatch(release.get('baseline_version', '')):
        raise ValueError('Invalid baseline version')
    seed = release_root / 'baseline'
    slug = release['baseline_slug']
    manifest = seed / 'Saved_Sessions' / 'profiles' / slug / 'manifest.json'
    if json.loads(manifest.read_text(encoding='utf-8-sig'))['version'] != release['baseline_version']:
        raise ValueError('Baseline manifest does not match the approved release')
    storage = Path(local_root) if local_root is not None else Path(os.environ['LOCALAPPDATA']) / 'ThermalAnalysisDashboard'
    storage = storage.resolve()
    if os.name == 'nt' and not str(storage).startswith('\\\\?\\'):
        absolute = str(storage)
        storage = Path('\\\\?\\UNC\\' + absolute[2:] if absolute.startswith('\\\\') else '\\\\?\\' + absolute)
    storage.mkdir(parents=True, exist_ok=True)
    workspace = storage / release['release_id']
    if not workspace.exists():
        staging = storage / f'.{uuid.uuid4().hex}.tmp'
        try:
            shutil.copytree(seed, staging)
            for folder in ('input', 'output', 'Profiles', 'Saved_Sessions'):
                (staging / folder).mkdir(exist_ok=True)
            try:
                staging.rename(workspace)
            except OSError:
                if not workspace.is_dir():
                    raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return workspace, release


def launch_desktop(release_root, port=8063, no_browser=False, local_root=None):
    import webbrowser
    from werkzeug.serving import make_server
    from shared_dashboard import create_app, publish_portal

    workspace, release = prepare_workspace(release_root, local_root)
    os.chdir(workspace)
    destination = workspace / 'output' / 'shared'
    publish_portal(workspace, destination)
    app = create_app(workspace, destination)
    app.config['BASELINE_SLUG'] = release['baseline_slug']

    try:
        server = make_server('127.0.0.1', port, app, threaded=True)
    except SystemExit:
        if port == 0:
            raise
        server = make_server('127.0.0.1', 0, app, threaded=True)
    url = f'http://127.0.0.1:{server.server_port}/?profile={release["baseline_slug"]}'
    print(f'Report workspace: {url}', flush=True)
    print(f'Private files: {workspace}', flush=True)
    print(f'Approved baseline: {release["baseline_title"]}', flush=True)
    if not no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()