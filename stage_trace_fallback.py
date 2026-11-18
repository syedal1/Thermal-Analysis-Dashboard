"""Stage normalized TRACE MCP exports as a report-matched AQUA fallback."""

import argparse
import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path


def stage(root, slug, sources, retrieved_at):
    root = Path(root).resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", slug):
        raise ValueError("Invalid report slug")
    profile_path = (root / "Profiles" / f"{slug}.json").resolve()
    if not profile_path.is_relative_to(root) or not profile_path.is_file() or profile_path.is_symlink():
        raise ValueError("Report definition was not found inside the application folder")
    profile = json.loads(profile_path.read_text(encoding="utf-8-sig"))
    fields = {key: profile.get(key, "") for key in (
        "slug", "lots", "test_program", "product", "subflow_step", "data_domain")}
    if fields['lots']:
        fields['test_program'] = ''
    try:
        timestamp = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("retrieved_at must be a timezone-aware ISO-8601 timestamp") from error
    if timestamp.tzinfo is None:
        raise ValueError("retrieved_at must include a timezone")
    fallback = root / "input" / "profiles" / slug / "trace_fallback"
    fallback.mkdir(parents=True, exist_ok=True)
    records = []
    for index, value in enumerate(sources):
        source = Path(value)
        if source.is_symlink():
            raise ValueError("Provide regular, non-symlink export files")
        source = source.resolve()
        if not source.is_file() or source.suffix.lower() not in (".csv", ".tsv", ".txt"):
            raise ValueError("Provide normalized regular CSV, TSV, or TXT exports")
        from large_data_store import _source_rows

        row_count = 0
        for row in _source_rows(source):
            row_count += 1
            lot = str(row.get("Lot", row.get("LOTS", "")) or "").strip()
            if fields["lots"] and lot not in fields["lots"]:
                raise ValueError("TRACE export contains a missing or unexpected lot")
            program = str(row.get("Program_Name", "") or "").strip()
            if fields["test_program"] and program != fields["test_program"]:
                raise ValueError("TRACE export contains a missing or unexpected test program")
        if not row_count:
            raise ValueError("TRACE export has no data rows")
        name = f"trace-{index}-{source.name}"
        target = fallback / name
        digest = hashlib.sha256()
        temporary = target.with_name(f".{uuid.uuid4().hex}.tmp")
        try:
            with source.open("rb") as original, temporary.open("xb") as stream:
                for block in iter(lambda: original.read(1024 * 1024), b""):
                    digest.update(block)
                    stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        records.append({"path": name, "sha256": digest.hexdigest()})
    if not records:
        raise ValueError("At least one TRACE export is required")
    receipt = {"source": "trace", "transport": "TRACE MCP staged export",
               "retrieved_at": timestamp.isoformat(),
               "profile": fields, "files": records}
    temporary = fallback / f".{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(receipt, stream, ensure_ascii=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, fallback / "retrieval.json")
    finally:
        temporary.unlink(missing_ok=True)
    return fallback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", help="Saved report slug")
    parser.add_argument("files", nargs="+", type=Path, help="Normalized TRACE MCP CSV/TSV/TXT exports")
    parser.add_argument("--retrieved-at", required=True, help="Timezone-aware TRACE retrieval timestamp in ISO-8601")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    print(stage(args.root, args.slug, args.files, args.retrieved_at))


if __name__ == "__main__":
    main()