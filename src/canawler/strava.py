"""Parsing for Strava account-export metadata and private GPS track files."""

from __future__ import annotations

import csv
import gzip
import math
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

import fitdecode
import polars as pl

from canawler.activities import (
    Activity,
    ActivityBuildError,
    ActivityIssue,
    GPSObservation,
)

TYPE_MAPPING = {"Run": "run", "Ride": "bike", "Hike": "hike"}
FIT_SEMICIRCLE_TO_DEGREES = 180.0 / 2**31


class StravaExportError(ActivityBuildError):
    """The Strava CSV/export layout is unusable or ambiguous."""


class TrackParseError(ActivityBuildError):
    """An individual GPS track cannot be parsed."""


@dataclass(frozen=True)
class StravaCatalog:
    export_directory: Path
    total_rows: int
    type_counts: dict[str, int]
    candidate_rows: int
    activities: tuple[Activity, ...]
    issues: tuple[ActivityIssue, ...]

    def track_path(self, activity: Activity) -> Path:
        if not activity.filename:
            raise StravaExportError("activity has no Filename")
        root = self.export_directory.resolve()
        candidate = (root / activity.filename).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise StravaExportError(
                f"Filename escapes the Strava export: {activity.filename}"
            ) from error
        return candidate


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _detail_column(frame: pl.DataFrame, base: str) -> str:
    duplicate = f"{base}.1"
    return duplicate if duplicate in frame.columns else base


def _validate_duplicate_columns(frame: pl.DataFrame, raw_header: list[str]) -> None:
    """Verify the observed Strava summary/detail duplicate-column semantics."""
    duplicate_counts = Counter(raw_header)
    for name in ("Elapsed Time", "Distance", "Max Heart Rate"):
        if duplicate_counts[name] > 1 and f"{name}.1" not in frame.columns:
            raise StravaExportError(
                f"duplicate {name!r} column was not exposed as {name + '.1'!r}"
            )

    if "Elapsed Time.1" in frame:
        pairs = frame.select("Elapsed Time", "Elapsed Time.1").drop_nulls()
        if (
            pairs.height
            and not pairs.select(
                (
                    (pl.col("Elapsed Time") - pl.col("Elapsed Time.1")).abs() <= 1e-6
                ).all()
            ).item()
        ):
            raise StravaExportError(
                "duplicate Elapsed Time columns disagree; field choice is ambiguous"
            )
    if "Distance.1" in frame:
        pairs = (
            frame.select("Distance", "Distance.1")
            .drop_nulls()
            .filter((pl.col("Distance").abs() > 0) | (pl.col("Distance.1").abs() > 0))
        )
        if pairs.height:
            valid = pairs.select(
                (
                    (pl.col("Distance.1") - pl.col("Distance") * 1000).abs()
                    <= 10 + pl.col("Distance.1").abs() * 0.01
                ).all()
            ).item()
            if not valid:
                raise StravaExportError(
                    "duplicate Distance columns do not have expected km/metre semantics"
                )
    if "Max Heart Rate.1" in frame:
        pairs = frame.select("Max Heart Rate", "Max Heart Rate.1").drop_nulls()
        if (
            pairs.height
            and not pairs.select(
                (
                    (pl.col("Max Heart Rate") - pl.col("Max Heart Rate.1")).abs()
                    <= 1e-6
                ).all()
            ).item()
        ):
            raise StravaExportError(
                "duplicate Max Heart Rate columns disagree; field choice is ambiguous"
            )


def _required_columns(frame: pl.DataFrame) -> None:
    required = {
        "Activity ID",
        "Activity Date",
        "Activity Name",
        "Activity Type",
        "Filename",
        "Elapsed Time",
        "Moving Time",
        "Distance",
        "Elevation Gain",
        "Elevation Loss",
        "Average Speed",
        "Max Speed",
        "Average Heart Rate",
        "Max Heart Rate",
        "Calories",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise StravaExportError(
            "activities.csv is missing required columns: " + ", ".join(missing)
        )


def load_catalog(export_directory: Path) -> StravaCatalog:
    export_directory = Path(export_directory)
    csv_path = export_directory / "activities.csv"
    if not csv_path.is_file():
        raise StravaExportError(f"activities.csv not found under {export_directory}")
    try:
        with csv_path.open(encoding="utf-8-sig", newline="") as stream:
            raw_header = next(csv.reader(stream))
        duplicate_counts: Counter[str] = Counter()
        columns = []
        for name in raw_header:
            occurrence = duplicate_counts[name]
            columns.append(name if occurrence == 0 else f"{name}.{occurrence}")
            duplicate_counts[name] += 1
        frame = pl.read_csv(
            csv_path,
            has_header=False,
            skip_rows=1,
            new_columns=columns,
            infer_schema_length=None,
        )
    except (OSError, StopIteration, pl.exceptions.PolarsError) as error:
        raise StravaExportError(f"could not read {csv_path}: {error}") from error

    _required_columns(frame)
    _validate_duplicate_columns(frame, raw_header)
    elapsed_column = _detail_column(frame, "Elapsed Time")
    distance_column = _detail_column(frame, "Distance")
    max_hr_column = _detail_column(frame, "Max Heart Rate")

    type_counts = dict(
        Counter(
            _optional_text(value) or "(missing)"
            for value in frame.get_column("Activity Type")
        )
    )
    candidate_frame = frame.filter(pl.col("Activity Type").is_in(list(TYPE_MAPPING)))
    candidate_rows = candidate_frame.height
    duplicate_ids = set(
        candidate_frame.filter(pl.col("Activity ID").is_duplicated())
        .get_column("Activity ID")
        .drop_nulls()
    )

    issues: list[ActivityIssue] = []
    activities: list[Activity] = []
    for row in candidate_frame.iter_rows(named=True):
        raw_activity_id = row["Activity ID"]
        try:
            activity_id = int(raw_activity_id)
        except (TypeError, ValueError, OverflowError):
            issues.append(
                ActivityIssue(None, "invalid_activity_id", repr(row["Activity ID"]))
            )
            continue
        filename = _optional_text(row["Filename"])
        if raw_activity_id in duplicate_ids:
            issues.append(
                ActivityIssue(
                    activity_id,
                    "duplicate_activity_id",
                    "all rows with this Activity ID were skipped",
                    filename,
                )
            )
            continue
        try:
            date_text = _optional_text(row["Activity Date"])
            if date_text is None:
                raise ValueError("activity date is missing")
            try:
                timestamp = datetime.strptime(
                    date_text, "%b %d, %Y, %I:%M:%S %p"
                ).replace(tzinfo=UTC)
            except ValueError:
                timestamp = datetime.fromisoformat(date_text)
                timestamp = (
                    timestamp.replace(tzinfo=UTC)
                    if timestamp.tzinfo is None
                    else timestamp.astimezone(UTC)
                )
        except (TypeError, ValueError, OverflowError) as error:
            issues.append(
                ActivityIssue(
                    activity_id,
                    "invalid_activity_date",
                    str(error),
                    filename,
                )
            )
            continue
        if filename is None:
            issues.append(
                ActivityIssue(
                    activity_id,
                    "missing_filename",
                    "CSV Filename is empty",
                )
            )
        else:
            root = export_directory.resolve()
            track_path = (root / filename).resolve()
            try:
                track_path.relative_to(root)
            except ValueError:
                issues.append(
                    ActivityIssue(
                        activity_id,
                        "unsafe_filename",
                        "CSV Filename escapes the export directory",
                        filename,
                    )
                )
                filename = None
            else:
                if not track_path.is_file():
                    issues.append(
                        ActivityIssue(
                            activity_id,
                            "missing_gps_file",
                            "CSV Filename does not exist",
                            filename,
                        )
                    )

        detail_distance = _optional_float(row[distance_column])
        distance_meters = (
            detail_distance
            if distance_column.endswith(".1") or detail_distance is None
            else detail_distance * 1000
        )
        activity_type = str(row["Activity Type"])
        activities.append(
            Activity(
                activity_id=activity_id,
                activity_datetime=timestamp,
                activity_name=str(row["Activity Name"]),
                original_activity_type=activity_type,
                normalized_activity_type=TYPE_MAPPING[activity_type],
                description=_optional_text(row.get("Activity Description")),
                elapsed_time_seconds=_optional_float(row[elapsed_column]),
                moving_time_seconds=_optional_float(row["Moving Time"]),
                distance_meters=distance_meters,
                elevation_gain_meters=_optional_float(row["Elevation Gain"]),
                elevation_loss_meters=_optional_float(row["Elevation Loss"]),
                average_speed_mps=_optional_float(row["Average Speed"]),
                max_speed_mps=_optional_float(row["Max Speed"]),
                average_heart_rate=_optional_float(row["Average Heart Rate"]),
                max_heart_rate=_optional_float(row[max_hr_column]),
                calories=_optional_float(row["Calories"]),
                filename=filename,
            )
        )
    return StravaCatalog(
        export_directory=export_directory,
        total_rows=frame.height,
        type_counts=type_counts,
        candidate_rows=candidate_rows,
        activities=tuple(activities),
        issues=tuple(issues),
    )


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or UTC)


def _valid_observation(
    timestamp: datetime | None, latitude: object, longitude: object
) -> GPSObservation | None:
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return GPSObservation(timestamp, latitude, longitude)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_tcx(stream: BinaryIO) -> tuple[GPSObservation, ...]:
    data = stream.read().lstrip()
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise TrackParseError("TCX contains a prohibited DTD/entity declaration")
    root = ET.fromstring(data)
    observations: list[GPSObservation] = []
    for trackpoint in root.iter():
        if _local_name(trackpoint.tag) != "Trackpoint":
            continue
        values = {
            _local_name(element.tag): element.text for element in trackpoint.iter()
        }
        observation = _valid_observation(
            _timestamp(values.get("Time")),
            values.get("LatitudeDegrees"),
            values.get("LongitudeDegrees"),
        )
        if observation is not None:
            observations.append(observation)
    return tuple(observations)


def _parse_gpx(stream: BinaryIO) -> tuple[GPSObservation, ...]:
    data = stream.read().lstrip()
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise TrackParseError("GPX contains a prohibited DTD/entity declaration")
    root = ET.fromstring(data)
    observations: list[GPSObservation] = []
    for point in root.iter():
        if _local_name(point.tag) not in {"trkpt", "rtept"}:
            continue
        time_text = next(
            (child.text for child in point if _local_name(child.tag) == "time"),
            None,
        )
        observation = _valid_observation(
            _timestamp(time_text), point.get("lat"), point.get("lon")
        )
        if observation is not None:
            observations.append(observation)
    return tuple(observations)


def _parse_fit(stream: BinaryIO) -> tuple[GPSObservation, ...]:
    observations: list[GPSObservation] = []
    with fitdecode.FitReader(
        stream,
        check_crc=fitdecode.CrcCheck.RAISE,
        error_handling=fitdecode.ErrorHandling.RAISE,
    ) as fit:
        for frame in fit:
            if frame.frame_type != fitdecode.FIT_FRAME_DATA or frame.name != "record":
                continue
            latitude = frame.get_value("position_lat", fallback=None)
            longitude = frame.get_value("position_long", fallback=None)
            timestamp = frame.get_value("timestamp", fallback=None)
            if latitude is None or longitude is None:
                continue
            if isinstance(timestamp, datetime) and timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            observation = _valid_observation(
                timestamp,
                latitude * FIT_SEMICIRCLE_TO_DEGREES,
                longitude * FIT_SEMICIRCLE_TO_DEGREES,
            )
            if observation is not None:
                observations.append(observation)
    return tuple(observations)


def load_track(path: Path) -> tuple[GPSObservation, ...]:
    path = Path(path)
    lowercase_name = path.name.casefold()
    if lowercase_name.endswith(".tcx.gz"):
        parser = _parse_tcx
    elif lowercase_name.endswith(".gpx.gz"):
        parser = _parse_gpx
    elif lowercase_name.endswith(".fit.gz"):
        parser = _parse_fit
    else:
        raise TrackParseError(f"unsupported GPS file extension: {path.name}")
    try:
        with gzip.open(path, "rb") as stream:
            return parser(stream)
    except TrackParseError:
        raise
    except (OSError, EOFError, ET.ParseError, fitdecode.FitError) as error:
        raise TrackParseError(f"could not parse {path.name}: {error}") from error


__all__ = [
    "FIT_SEMICIRCLE_TO_DEGREES",
    "TYPE_MAPPING",
    "StravaCatalog",
    "StravaExportError",
    "TrackParseError",
    "load_catalog",
    "load_track",
]
