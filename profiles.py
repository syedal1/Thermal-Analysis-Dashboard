"""Work-week profile management: saved Aqua/Trace query parameters with an
isolated DuckDB cache per profile.

Each profile is a small JSON file in Profiles/ describing which work week,
product, test program and lot(s) to pull. Its pulled source files live under
input/profiles/<slug>/ and its crunched data lives in its own DuckDB database
under Saved_Sessions/profiles/<slug>.duckdb, fully isolated from the main
dataset and from other profiles.
"""

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

PROFILES_FOLDER = Path("Profiles")
PROFILE_INPUT_ROOT = Path("input") / "profiles"
PROFILE_DB_ROOT = Path("Saved_Sessions") / "profiles"

_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")


class ProfileError(Exception):
    """Raised for invalid profile names or missing profile files."""


class AquaTraceNotConfigured(Exception):
    """Raised when no Aqua/Trace data-fetch connector has been plugged in yet."""


def slugify(name):
    """Turn a user-supplied work week label into a safe, unique filesystem slug."""
    slug = _SLUG_RE.sub("-", (name or "").strip()).strip("-")
    if not slug:
        raise ProfileError("Work Week must contain at least one letter, digit, '-' or '_'.")
    return slug


def _profile_path(slug):
    return PROFILES_FOLDER / f"{slug}.json"


def profile_input_folder(slug):
    return PROFILE_INPUT_ROOT / slug


def profile_database_path(slug):
    return PROFILE_DB_ROOT / f"{slug}.duckdb"


def list_profiles():
    """Return all saved profiles, most recently updated work week first."""
    PROFILES_FOLDER.mkdir(parents=True, exist_ok=True)
    profiles = []
    for path in sorted(PROFILES_FOLDER.glob("*.json")):
        try:
            profiles.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    profiles.sort(key=lambda p: p.get("updated", ""), reverse=True)
    return profiles


def load_profile(slug):
    slug = slugify(slug)
    path = _profile_path(slug)
    if not path.is_file():
        raise ProfileError(f"Profile '{slug}' does not exist.")
    return json.loads(path.read_text(encoding="utf-8"))


def save_profile(work_week, product, test_program, lots, notes=""):
    """Create or update a profile definition. Returns the saved profile dict."""
    slug = slugify(work_week)
    PROFILES_FOLDER.mkdir(parents=True, exist_ok=True)
    existing = {}
    if _profile_path(slug).is_file():
        existing = load_profile(slug)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    profile = {
        "slug": slug,
        "work_week": (work_week or "").strip(),
        "product": product or "",
        "test_program": (test_program or "").strip(),
        "lots": [lot.strip() for lot in re.split(r"[,\s]+", lots or "") if lot.strip()],
        "notes": notes or "",
        "created": existing.get("created", now),
        "updated": now,
        "last_pull_status": existing.get("last_pull_status", "Never pulled"),
        "last_pull_time": existing.get("last_pull_time"),
    }
    _profile_path(slug).write_text(json.dumps(profile, indent=2), encoding="utf-8")
    return profile


def mark_pull_result(slug, status):
    """Record the outcome of the most recent pull/crunch attempt on a profile."""
    profile = load_profile(slug)
    profile["last_pull_status"] = status
    profile["last_pull_time"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _profile_path(slug).write_text(json.dumps(profile, indent=2), encoding="utf-8")
    return profile


def delete_profile(slug):
    """Remove a profile's definition, pulled input files and cached database."""
    slug = slugify(slug)
    _profile_path(slug).unlink(missing_ok=True)
    shutil.rmtree(profile_input_folder(slug), ignore_errors=True)
    db_path = profile_database_path(slug)
    shutil.rmtree(db_path.with_suffix(".spill"), ignore_errors=True)
    db_path.unlink(missing_ok=True)
    Path(f"{db_path}.wal").unlink(missing_ok=True)


# ----------------------------------------------------------------------------
# Aqua/Trace data pull - pluggable connector
# ----------------------------------------------------------------------------
# No Aqua/Trace API/CLI details are available yet. To enable automatic pulls,
# set AQUA_TRACE_FETCHER to a callable with this signature:
#   fetcher(lots: list[str], test_program: str, product: str, dest_folder: Path) -> list[Path]
# It must write the matching readings as CSV/TXT/XLSX file(s) into dest_folder
# using the same columns the dashboard already ingests from input/ (TEST_NAME,
# TEST_RESULT, VISUAL_ID, Lot, DevRevStep, ...) and return the files it wrote.
AQUA_TRACE_FETCHER = None


def pull_profile_data(profile):
    """Fetch this profile's data from Aqua/Trace into its isolated input folder.

    Raises AquaTraceNotConfigured until a real connector is plugged into
    AQUA_TRACE_FETCHER. Until then, exported files can be copied manually into
    the folder returned by profile_input_folder() and crunched from the UI.
    """
    dest = profile_input_folder(profile["slug"])
    dest.mkdir(parents=True, exist_ok=True)
    if AQUA_TRACE_FETCHER is None:
        raise AquaTraceNotConfigured(
            "No Aqua/Trace connector is configured yet. Set profiles.AQUA_TRACE_FETCHER "
            f"to a fetch function, or copy exported files into {dest} and click Pull/Refresh."
        )
    return AQUA_TRACE_FETCHER(profile["lots"], profile["test_program"], profile["product"], dest)
