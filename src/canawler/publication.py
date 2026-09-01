"""Select and write the privacy-safe data consumed by the website."""

from __future__ import annotations

import csv
import json
from io import StringIO
from pathlib import Path
from typing import Any

PUBLIC_ACTIVITY_FIELDS = (
    "activity_id",
    "date",
    "activity_name",
    "activity_type",
    "strava_distance_miles",
    "co_travel_miles",
    "co_unique_miles",
    "new_co_unique_miles",
    "cumulative_unique_miles",
    "cumulative_percent_complete",
    "min_milepost",
    "max_milepost",
    "moving_time_minutes",
    "elevation_gain_feet",
    "strava_url",
    "segments",
)
PUBLIC_ACTIVITY_COLUMNS = PUBLIC_ACTIVITY_FIELDS[:-1]

PUBLIC_COVERAGE_FIELDS = (
    "canal_miles",
    "bin_miles",
    "combined_unique_miles",
    "combined_percent_complete",
    "remaining_miles",
    "run_unique_miles",
    "bike_unique_miles",
    "hike_unique_miles",
    "activity_count",
    "first_activity_date",
    "latest_activity_date",
    "longest_continuous_completed_section",
    "completed_segments",
    "remaining_segments",
    "run_segments",
    "bike_segments",
    "hike_segments",
    "methodology",
)


def _write_if_changed(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text() != text:
        path.write_text(text)


def publish_artifacts(
    activity_records: list[dict[str, Any]],
    coverage: dict[str, Any],
    output_directory: Path = Path("data/public"),
) -> tuple[Path, Path, Path]:
    """Write deterministic public artifacts using explicit privacy allowlists."""
    public_activities = [
        {field: record[field] for field in PUBLIC_ACTIVITY_FIELDS}
        for record in activity_records
    ]
    public_coverage = {field: coverage[field] for field in PUBLIC_COVERAGE_FIELDS}

    output_directory = Path(output_directory)
    csv_path = output_directory / "activities.csv"
    activities_json_path = output_directory / "activities.json"
    coverage_json_path = output_directory / "coverage.json"

    stream = StringIO(newline="")
    writer = csv.DictWriter(
        stream, fieldnames=PUBLIC_ACTIVITY_COLUMNS, lineterminator="\n"
    )
    writer.writeheader()
    for record in public_activities:
        writer.writerow({field: record[field] for field in PUBLIC_ACTIVITY_COLUMNS})

    _write_if_changed(csv_path, stream.getvalue())
    _write_if_changed(
        activities_json_path,
        json.dumps(public_activities, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n",
    )
    _write_if_changed(
        coverage_json_path,
        json.dumps(public_coverage, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n",
    )
    return csv_path, activities_json_path, coverage_json_path


__all__ = [
    "PUBLIC_ACTIVITY_COLUMNS",
    "PUBLIC_ACTIVITY_FIELDS",
    "PUBLIC_COVERAGE_FIELDS",
    "publish_artifacts",
]
