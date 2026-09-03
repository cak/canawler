"""Select and write the privacy-safe data consumed by the website."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from canawler.csv_export import (
    PUBLIC_ACTIVITY_COLUMNS,
    CsvExportSummary,
    export_public_csvs,
)
from canawler.reference import PUBLIC_DIR, public_format_directories

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
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")


def _combined_features(
    access_points: dict[str, Any], locks: dict[str, Any]
) -> dict[str, Any]:
    """Combine the rich feature records without changing their source schemas."""
    features = [
        *(
            {"feature_type": "access_point", **record}
            for record in access_points["access_points"]
        ),
        *({"feature_type": "lock", **record} for record in locks["locks"]),
    ]
    features.sort(
        key=lambda record: (
            record["milepost"],
            record["feature_type"],
            record["name"],
            record["id"],
        )
    )
    return {
        "schema_version": access_points["schema_version"],
        "features": features,
    }


def publish_artifacts(
    activity_records: list[dict[str, Any]],
    coverage: dict[str, Any],
    output_directory: Path = PUBLIC_DIR,
) -> tuple[tuple[Path, ...], CsvExportSummary]:
    """Write canonical JSON, then derive deterministic CSV from that JSON."""
    public_activities = [
        {field: record[field] for field in PUBLIC_ACTIVITY_FIELDS}
        for record in activity_records
    ]
    public_coverage = {field: coverage[field] for field in PUBLIC_COVERAGE_FIELDS}

    output_directory = Path(output_directory)
    json_directory, _ = public_format_directories(output_directory)
    activities_json_path = json_directory / "activities.json"
    coverage_json_path = json_directory / "coverage.json"
    access_points_path = json_directory / "access-points.json"
    features_path = json_directory / "features.json"
    locks_path = json_directory / "locks.json"
    sources_path = json_directory / "sources.json"
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
    from canawler.reference_artifacts import build_public_reference_artifacts

    public_access_points, public_locks = build_public_reference_artifacts(
        activity_records, coverage
    )
    _write_if_changed(
        access_points_path,
        json.dumps(public_access_points, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n",
    )
    _write_if_changed(
        locks_path,
        json.dumps(public_locks, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    )
    _write_if_changed(
        features_path,
        json.dumps(
            _combined_features(public_access_points, public_locks),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n",
    )
    from canawler.provenance import build_public_source_registry

    _write_if_changed(
        sources_path,
        json.dumps(
            build_public_source_registry(),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n",
    )
    analytics = export_public_csvs(output_directory)
    return (
        (
            activities_json_path,
            coverage_json_path,
            access_points_path,
            features_path,
            locks_path,
            sources_path,
            *analytics.output_paths,
        ),
        analytics,
    )


__all__ = [
    "PUBLIC_ACTIVITY_COLUMNS",
    "PUBLIC_ACTIVITY_FIELDS",
    "PUBLIC_COVERAGE_FIELDS",
    "publish_artifacts",
]
