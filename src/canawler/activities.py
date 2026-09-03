"""Activity records and orchestration for a complete historical rebuild."""

from __future__ import annotations

import csv
import json
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from canawler.csv_export import CsvExportSummary

REFERENCE_PATH = Path("data/reference/co-towpath/towpath.geojson")
PROCESSED_DIRECTORY = Path("data/processed")
TRACK_WORKERS = 4


@dataclass(frozen=True)
class GPSObservation:
    timestamp: datetime | None
    latitude: float
    longitude: float


@dataclass(frozen=True)
class Activity:
    activity_id: int
    activity_datetime: datetime
    activity_name: str
    original_activity_type: str
    normalized_activity_type: str
    description: str | None
    elapsed_time_seconds: float | None
    moving_time_seconds: float | None
    distance_meters: float | None
    elevation_gain_meters: float | None
    elevation_loss_meters: float | None
    average_speed_mps: float | None
    max_speed_mps: float | None
    average_heart_rate: float | None
    max_heart_rate: float | None
    calories: float | None
    filename: str | None


@dataclass(frozen=True)
class ActivityIssue:
    activity_id: int | None
    code: str
    message: str
    filename: str | None = None


@dataclass(frozen=True)
class BuildReport:
    candidate_activities: int
    parsed_tracks: int
    covered_activities: int
    covered_type_counts: dict[str, int]
    combined_unique_miles: float
    completion_percentage: float
    output_paths: tuple[Path, ...]
    public_analytics: CsvExportSummary
    issues: tuple[ActivityIssue, ...]

    def format(self) -> str:
        types = ", ".join(
            f"{name}={self.covered_type_counts.get(name, 0)}"
            for name in ("run", "bike", "hike")
        )
        lines = [
            f"Candidate activities: {self.candidate_activities}",
            f"Successfully parsed tracks: {self.parsed_tracks}",
            f"Activities with C&O coverage: {self.covered_activities}",
            f"Covered activity counts: {types}",
            f"Combined unique C&O miles: {self.combined_unique_miles:.2f}",
            f"Completion: {self.completion_percentage:.2f}%",
            self.public_analytics.format(),
        ]
        lines.extend(f"Output: {path}" for path in self.output_paths)
        if self.issues:
            counts = Counter(issue.code for issue in self.issues)
            lines.append(
                "Input issues: "
                + ", ".join(f"{code}={count}" for code, count in sorted(counts.items()))
            )
        return "\n".join(lines)


class ActivityBuildError(RuntimeError):
    """The historical activity build cannot proceed safely."""


CSV_COLUMNS = (
    "activity_id",
    "date",
    "start_time",
    "datetime",
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
    "elapsed_time_minutes",
    "elevation_gain_feet",
    "average_heart_rate",
    "max_heart_rate",
    "calories",
    "matched_point_count",
    "total_gps_point_count",
    "match_percentage",
    "strava_url",
)

_WORKER_ENGINE: Any = None


def discover_strava_export(
    export_directory: Path | None = None,
    search_root: Path = Path("data/raw/strava"),
) -> Path:
    if export_directory is not None:
        return Path(export_directory)
    matches = sorted(
        path.parent
        for path in Path(search_root).glob("*/activities.csv")
        if path.is_file()
    )
    if len(matches) != 1:
        raise ActivityBuildError(
            "expected exactly one Strava export directory containing activities.csv "
            f"under {search_root}; found {len(matches)} (use --export to choose one)"
        )
    return matches[0]


def _write_if_changed(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")


def _write_processed_outputs(
    output_directory: Path,
    activity_records: list[dict[str, Any]],
    coverage: dict[str, Any],
) -> tuple[Path, Path, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    csv_path = output_directory / "activities.csv"
    activities_json_path = output_directory / "activities.json"
    coverage_json_path = output_directory / "coverage.json"

    from io import StringIO

    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for record in activity_records:
        writer.writerow({column: record.get(column) for column in CSV_COLUMNS})
    _write_if_changed(csv_path, stream.getvalue())
    _write_if_changed(
        activities_json_path,
        json.dumps(activity_records, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n",
    )
    _write_if_changed(
        coverage_json_path,
        json.dumps(coverage, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    )
    return csv_path, activities_json_path, coverage_json_path


def _initialize_worker(reference_path: Path) -> None:
    from canawler.coverage import CoverageEngine

    global _WORKER_ENGINE
    _WORKER_ENGINE = CoverageEngine.from_geojson(reference_path)


def _parse_and_match(
    job: tuple[Activity, Path],
) -> tuple[str, Activity, Any | str | None]:
    from canawler.strava import TrackParseError, load_track

    activity, path = job
    try:
        observations = load_track(path)
    except TrackParseError as error:
        return "parse_error", activity, str(error)
    if not observations:
        return "no_coordinates", activity, None
    if _WORKER_ENGINE is None:
        raise ActivityBuildError("coverage worker was not initialized")
    return "matched", activity, _WORKER_ENGINE.match(observations)


def build_historical_activities(
    export_directory: Path | None = None,
    *,
    reference_path: Path = REFERENCE_PATH,
    processed_directory: Path = PROCESSED_DIRECTORY,
) -> BuildReport:
    from canawler.coverage import (
        calculate_history,
        canonical_reference_sha256,
    )
    from canawler.publication import publish_artifacts
    from canawler.reference import (
        PUBLIC_DIR,
        public_format_directories,
        validate_canonical_reference,
    )
    from canawler.strava import load_catalog

    export_directory = discover_strava_export(export_directory)
    validate_canonical_reference(reference_path)
    catalog = load_catalog(export_directory)
    issues = list(catalog.issues)
    jobs = []
    for activity in sorted(
        catalog.activities,
        key=lambda item: (item.activity_datetime, item.activity_id),
    ):
        if not activity.filename:
            continue
        track_path = catalog.track_path(activity)
        if track_path.is_file():
            jobs.append((activity, track_path))
    results = []
    if jobs:
        with ProcessPoolExecutor(
            max_workers=min(TRACK_WORKERS, len(jobs)),
            initializer=_initialize_worker,
            initargs=(reference_path,),
        ) as executor:
            results = list(executor.map(_parse_and_match, jobs, chunksize=4))

    matched = []
    parsed_tracks = 0
    for status, activity, value in results:
        if status == "parse_error":
            issues.append(
                ActivityIssue(
                    activity.activity_id,
                    "track_parse_failure",
                    str(value),
                    activity.filename,
                )
            )
            continue
        parsed_tracks += 1
        if status == "no_coordinates":
            issues.append(
                ActivityIssue(
                    activity.activity_id,
                    "no_gps_coordinates",
                    "track parsed but contains no usable coordinates",
                    activity.filename,
                )
            )
            continue
        if value.covered_bins:
            matched.append((activity, value))

    records, coverage = calculate_history(
        matched,
        reference_sha256=canonical_reference_sha256(reference_path),
    )
    processed_paths = _write_processed_outputs(processed_directory, records, coverage)
    public_directory = Path(processed_directory).parent / PUBLIC_DIR.name
    public_paths, public_analytics = publish_artifacts(
        records, coverage, public_directory
    )
    from canawler.visualization import build_coverage_map

    _, public_csv_directory = public_format_directories(public_directory)
    visualization_paths = build_coverage_map(
        reference_path=reference_path,
        coverage_path=public_csv_directory / "coverage-segments.csv",
    )
    type_counts = Counter(record["activity_type"] for record in records)
    return BuildReport(
        candidate_activities=catalog.candidate_rows,
        parsed_tracks=parsed_tracks,
        covered_activities=len(records),
        covered_type_counts=dict(type_counts),
        combined_unique_miles=coverage["combined_unique_miles"],
        completion_percentage=coverage["combined_percent_complete"],
        output_paths=processed_paths + public_paths + visualization_paths,
        public_analytics=public_analytics,
        issues=tuple(issues),
    )


def _format_audit_finding(record: dict[str, Any], reasons: tuple[str, ...]) -> str:
    distance = record.get("strava_distance_miles")
    strava = "unknown" if distance is None else f"{float(distance):.3f}"
    return (
        f"{record['activity_id']} | {record['date']} | {record['activity_type']} | "
        f"{record['activity_name']} | Strava={strava} mi | "
        f"C&O travel={record['co_travel_miles']:.3f} mi | "
        f"unique={record['co_unique_miles']:.2f} mi | "
        f"new={record['new_co_unique_miles']:.2f} mi | "
        f"MP {record['min_milepost']:.2f}-{record['max_milepost']:.2f} | "
        f"matched={record['match_percentage']:.2f}% | {','.join(reasons)}"
    )


def audit_historical_activities(export_directory: Path | None = None) -> str:
    """Rebuild coverage and report suspicious matches without changing results."""
    from canawler.coverage import audit_activity_record

    build = build_historical_activities(export_directory)
    records = json.loads(
        (PROCESSED_DIRECTORY / "activities.json").read_text(encoding="utf-8")
    )
    findings = [
        _format_audit_finding(record, reasons)
        for record in records
        if (reasons := audit_activity_record(record))
    ]
    lines = [
        f"Candidate activities audited: {build.candidate_activities}",
        f"Activities with C&O coverage: {build.covered_activities}",
        f"Combined unique C&O miles: {build.combined_unique_miles:.2f}",
        f"Completion: {build.completion_percentage:.3f}%",
        f"Activities flagged for review: {len(findings)}",
    ]
    lines.extend(f"  {finding}" for finding in findings)
    if build.issues:
        counts = Counter(issue.code for issue in build.issues)
        lines.append(
            "Input diagnostics: "
            + ", ".join(f"{code}={count}" for code, count in sorted(counts.items()))
        )
    return "\n".join(lines)


__all__ = [
    "CSV_COLUMNS",
    "Activity",
    "ActivityBuildError",
    "ActivityIssue",
    "BuildReport",
    "GPSObservation",
    "audit_historical_activities",
    "build_historical_activities",
    "discover_strava_export",
]
