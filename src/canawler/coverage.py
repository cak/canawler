"""Source-neutral matching of ordered GPS observations to the C&O linear reference."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from pyproj import Transformer
from shapely import STRtree, line_locate_point, points
from shapely.geometry import LineString, shape
from shapely.ops import substring, transform

from canawler.activities import Activity, GPSObservation

CANAL_MILES = 184.5
BIN_MILES = 0.01
BIN_COUNT = 18_450
METERS_PER_MILE = 1609.344
METERS_PER_FOOT = 0.3048
MATCH_CRS = "EPSG:5070"
MATCH_TOLERANCE_METERS = 30.0
MAX_SOURCE_GAP_METERS = 500.0
MAX_ALONG_GAP_METERS = 400.0
MAX_TIME_GAP_SECONDS = 180.0
MINIMUM_ALIGNMENT_RATIO = 0.65
MINIMUM_ALIGNMENT_DISTANCE_METERS = 5.0
MINIMUM_RUN_POINTS = 3
MINIMUM_RUN_TRAVEL_METERS = 32.0

LOW_MATCH_PERCENTAGE = 25.0
LOW_MATCHED_POINT_COUNT = 50
TRAVEL_EXCEEDS_STRAVA_TOLERANCE_MILES = 0.01
LARGE_TRAVEL_EXCESS_RATIO = 0.03
PARALLEL_MIN_MEDIAN_OFFSET_METERS = 12.0
PARALLEL_MIN_UNIQUE_MILES = 0.25
PARALLEL_REVIEW_INTERVALS = ((0.0, 3.5),)


class CoverageError(RuntimeError):
    """Coverage cannot be calculated safely from the supplied data."""


class CoverageInvariantError(CoverageError):
    """A calculated or serialized coverage result violates a structural invariant."""


@dataclass(frozen=True)
class CoverageSegment:
    start_milepost: float
    end_milepost: float
    miles: float

    def as_dict(self) -> dict[str, float]:
        return {
            "start_milepost": round(self.start_milepost, 2),
            "end_milepost": round(self.end_milepost, 2),
            "miles": round(self.miles, 2),
        }


@dataclass(frozen=True)
class ActivityMatch:
    co_travel_miles: float
    covered_bins: frozenset[int]
    segments: tuple[CoverageSegment, ...]
    matched_point_count: int
    total_gps_point_count: int
    median_match_distance_meters: float | None = None
    p90_match_distance_meters: float | None = None
    median_alignment_ratio: float | None = None


def bins_to_segments(
    bins: set[int] | frozenset[int],
) -> tuple[CoverageSegment, ...]:
    if not bins:
        return ()
    ordered = sorted(bins)
    runs: list[tuple[int, int]] = []
    start = previous = ordered[0]
    for bin_index in ordered[1:]:
        if bin_index != previous + 1:
            runs.append((start, previous))
            start = bin_index
        previous = bin_index
    runs.append((start, previous))
    return tuple(
        CoverageSegment(
            start * BIN_MILES,
            min((end + 1) * BIN_MILES, CANAL_MILES),
            (end - start + 1) * BIN_MILES,
        )
        for start, end in runs
    )


def milepost_intervals_to_geometries(
    reference: LineString,
    intervals: Iterable[tuple[float, float]],
) -> tuple[LineString, ...]:
    """Slice a route using Canawler's normalized 0–184.5 mile coordinate."""
    if (
        reference.geom_type != "LineString"
        or reference.is_empty
        or not reference.is_valid
        or reference.length <= 0
    ):
        raise CoverageError("reference geometry must be one valid, nonempty LineString")

    geometries: list[LineString] = []
    for index, interval in enumerate(intervals):
        try:
            start_raw, end_raw = interval
            start = float(start_raw)
            end = float(end_raw)
        except (TypeError, ValueError) as error:
            raise CoverageError(f"coverage interval {index} is malformed") from error
        if not math.isfinite(start) or not math.isfinite(end):
            raise CoverageError(f"coverage interval {index} must contain finite values")
        if not 0 <= start <= CANAL_MILES or not 0 <= end <= CANAL_MILES:
            raise CoverageError(
                f"coverage interval {index} is outside 0-{CANAL_MILES} miles"
            )
        if start >= end:
            raise CoverageError(
                f"coverage interval {index} start must be less than its end"
            )

        geometry = substring(
            reference,
            start / CANAL_MILES,
            end / CANAL_MILES,
            normalized=True,
        )
        if geometry.geom_type != "LineString" or geometry.is_empty:
            raise CoverageError(f"coverage interval {index} produced no line geometry")
        geometries.append(geometry)
    return tuple(geometries)


class CoverageEngine:
    def __init__(self, reference: LineString):
        if (
            reference.geom_type != "LineString"
            or reference.is_empty
            or not reference.is_valid
        ):
            raise CoverageError("reference geometry must be one valid WGS84 LineString")
        self._transformer = Transformer.from_crs("EPSG:4326", MATCH_CRS, always_xy=True)
        self._line = transform(self._transformer.transform, reference)
        if self._line.length <= 0:
            raise CoverageError("projected reference geometry has zero length")
        coordinates = list(self._line.coords)
        self._segments = np.asarray(
            [
                LineString((coordinates[index], coordinates[index + 1]))
                for index in range(len(coordinates) - 1)
            ],
            dtype=object,
        )
        segment_lengths = np.asarray(
            [segment.length for segment in self._segments], dtype=float
        )
        self._segment_starts = np.concatenate(
            (np.asarray([0.0]), np.cumsum(segment_lengths[:-1]))
        )
        self._segment_tree = STRtree(self._segments)

    @classmethod
    def from_geojson(cls, path: Path) -> CoverageEngine:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            features = payload["features"]
            if len(features) != 1:
                raise CoverageError(
                    "reference GeoJSON must contain exactly one feature"
                )
            geometry = shape(features[0]["geometry"])
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise CoverageError(
                f"could not read reference GeoJSON {path}: {error}"
            ) from error
        return cls(geometry)

    def _leg_is_plausible(
        self,
        left: int,
        right: int,
        observations: tuple[GPSObservation, ...],
        xs: np.ndarray,
        ys: np.ndarray,
        along_meters: np.ndarray,
        accepted: np.ndarray,
    ) -> bool:
        if not accepted[left] or not accepted[right]:
            return False
        source_distance = math.hypot(xs[right] - xs[left], ys[right] - ys[left])
        along_distance = abs(along_meters[right] - along_meters[left])
        if source_distance > MAX_SOURCE_GAP_METERS:
            return False
        if along_distance > MAX_ALONG_GAP_METERS:
            return False
        left_time = observations[left].timestamp
        right_time = observations[right].timestamp
        if left_time is not None and right_time is not None:
            time_gap = (right_time - left_time).total_seconds()
            if time_gap < 0 or time_gap > MAX_TIME_GAP_SECONDS:
                return False
        return not (
            source_distance >= MINIMUM_ALIGNMENT_DISTANCE_METERS
            and along_distance / source_distance < MINIMUM_ALIGNMENT_RATIO
        )

    def match(self, observations: tuple[GPSObservation, ...]) -> ActivityMatch:
        total_points = len(observations)
        if total_points < 2:
            return ActivityMatch(0.0, frozenset(), (), 0, total_points)

        longitudes = np.fromiter(
            (observation.longitude for observation in observations), dtype=float
        )
        latitudes = np.fromiter(
            (observation.latitude for observation in observations), dtype=float
        )
        xs_raw, ys_raw = self._transformer.transform(longitudes, latitudes)
        xs = np.asarray(xs_raw, dtype=float)
        ys = np.asarray(ys_raw, dtype=float)
        projected_points = points(xs, ys)
        nearest_indexes, offsets_raw = self._segment_tree.query_nearest(
            projected_points,
            return_distance=True,
            all_matches=False,
        )
        segment_indexes = nearest_indexes[1]
        offsets = np.asarray(offsets_raw, dtype=float)
        along_meters = self._segment_starts[segment_indexes] + np.asarray(
            line_locate_point(
                self._segments[segment_indexes],
                projected_points,
            ),
            dtype=float,
        )
        accepted = offsets <= MATCH_TOLERANCE_METERS
        matched_count = int(np.count_nonzero(accepted))
        mileposts = along_meters / self._line.length * CANAL_MILES

        plausible_legs = [
            self._leg_is_plausible(
                index,
                index + 1,
                observations,
                xs,
                ys,
                along_meters,
                accepted,
            )
            for index in range(total_points - 1)
        ]
        alignment_ratios = []
        for index, plausible in enumerate(plausible_legs):
            if not plausible:
                continue
            source_distance = math.hypot(
                xs[index + 1] - xs[index], ys[index + 1] - ys[index]
            )
            if source_distance >= MINIMUM_ALIGNMENT_DISTANCE_METERS:
                alignment_ratios.append(
                    abs(along_meters[index + 1] - along_meters[index]) / source_distance
                )
        runs: list[list[int]] = []
        current: list[int] = []
        for index, plausible in enumerate(plausible_legs):
            if plausible:
                current.append(index)
            elif current:
                runs.append(current)
                current = []
        if current:
            runs.append(current)

        covered_bins: set[int] = set()
        travel_miles = 0.0
        for run in runs:
            run_travel_meters = sum(
                abs(along_meters[index + 1] - along_meters[index]) for index in run
            )
            if len(run) + 1 < MINIMUM_RUN_POINTS:
                continue
            if run_travel_meters < MINIMUM_RUN_TRAVEL_METERS:
                continue
            for index in run:
                left = float(mileposts[index])
                right = float(mileposts[index + 1])
                travel_miles += abs(right - left)
                low, high = sorted((left, right))
                first_bin = max(0, math.floor(low / BIN_MILES))
                capped_high = min(high, CANAL_MILES - 1e-12)
                last_bin = min(
                    BIN_COUNT - 1,
                    math.floor(capped_high / BIN_MILES),
                )
                if last_bin >= first_bin:
                    covered_bins.update(range(first_bin, last_bin + 1))

        frozen_bins = frozenset(covered_bins)
        accepted_offsets = offsets[accepted]
        return ActivityMatch(
            co_travel_miles=travel_miles,
            covered_bins=frozen_bins,
            segments=bins_to_segments(frozen_bins),
            matched_point_count=matched_count,
            total_gps_point_count=total_points,
            median_match_distance_meters=(
                float(np.median(accepted_offsets)) if matched_count else None
            ),
            p90_match_distance_meters=(
                float(np.percentile(accepted_offsets, 90)) if matched_count else None
            ),
            median_alignment_ratio=(
                float(np.median(alignment_ratios)) if alignment_ratios else None
            ),
        )


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(float(value), digits)


def _iso_datetime(value: Any) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _activity_record(
    activity: Activity,
    match: ActivityMatch,
    new_bins: set[int],
    cumulative_bins: set[int],
) -> dict[str, Any]:
    segments = [segment.as_dict() for segment in match.segments]
    match_percentage = (
        match.matched_point_count / match.total_gps_point_count * 100
        if match.total_gps_point_count
        else 0.0
    )
    return {
        "activity_id": activity.activity_id,
        "date": activity.activity_datetime.date().isoformat(),
        "start_time": activity.activity_datetime.strftime("%H:%M:%SZ"),
        "datetime": _iso_datetime(activity.activity_datetime),
        "activity_name": activity.activity_name,
        "original_activity_type": activity.original_activity_type,
        "activity_type": activity.normalized_activity_type,
        "description": activity.description,
        "strava_distance_miles": _round(
            activity.distance_meters / METERS_PER_MILE
            if activity.distance_meters is not None
            else None,
            3,
        ),
        "co_travel_miles": round(match.co_travel_miles, 3),
        "co_unique_miles": round(len(match.covered_bins) * BIN_MILES, 2),
        "new_co_unique_miles": round(len(new_bins) * BIN_MILES, 2),
        "cumulative_unique_miles": round(len(cumulative_bins) * BIN_MILES, 2),
        "cumulative_percent_complete": round(
            len(cumulative_bins) * BIN_MILES / CANAL_MILES * 100, 3
        ),
        "min_milepost": segments[0]["start_milepost"],
        "max_milepost": segments[-1]["end_milepost"],
        "moving_time_minutes": _round(
            activity.moving_time_seconds / 60
            if activity.moving_time_seconds is not None
            else None,
            2,
        ),
        "elapsed_time_minutes": _round(
            activity.elapsed_time_seconds / 60
            if activity.elapsed_time_seconds is not None
            else None,
            2,
        ),
        "elevation_gain_feet": _round(
            activity.elevation_gain_meters / METERS_PER_FOOT
            if activity.elevation_gain_meters is not None
            else None,
            1,
        ),
        "elevation_loss_feet": _round(
            activity.elevation_loss_meters / METERS_PER_FOOT
            if activity.elevation_loss_meters is not None
            else None,
            1,
        ),
        "average_speed_mps": _round(activity.average_speed_mps, 3),
        "max_speed_mps": _round(activity.max_speed_mps, 3),
        "average_heart_rate": _round(activity.average_heart_rate, 1),
        "max_heart_rate": _round(activity.max_heart_rate, 1),
        "calories": _round(activity.calories, 1),
        "filename": activity.filename,
        "matched_point_count": match.matched_point_count,
        "total_gps_point_count": match.total_gps_point_count,
        "match_percentage": round(match_percentage, 2),
        "matching_diagnostics": {
            "median_distance_meters": _round(match.median_match_distance_meters, 2),
            "p90_distance_meters": _round(match.p90_match_distance_meters, 2),
            "median_alignment_ratio": _round(match.median_alignment_ratio, 3),
        },
        "strava_url": f"https://www.strava.com/activities/{activity.activity_id}",
        "segments": segments,
    }


def canonical_reference_sha256(path: Path) -> str:
    """Return a content hash without recording a machine-specific path."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as error:
        raise CoverageError(
            f"could not hash reference GeoJSON {path}: {error}"
        ) from error


def _serialized_segment_bins(segments: Any, label: str) -> set[int]:
    if not isinstance(segments, list):
        raise CoverageInvariantError(f"{label}: segments must be a list")
    result: set[int] = set()
    previous_end = 0.0
    for index, segment in enumerate(segments):
        try:
            start = float(segment["start_milepost"])
            end = float(segment["end_milepost"])
            miles = float(segment["miles"])
        except (KeyError, TypeError, ValueError) as error:
            raise CoverageInvariantError(
                f"{label}: segment {index} is malformed"
            ) from error
        if not 0 <= start < end <= CANAL_MILES:
            raise CoverageInvariantError(
                f"{label}: segment {index} is outside 0-{CANAL_MILES} miles"
            )
        if index and start < previous_end - 1e-9:
            raise CoverageInvariantError(f"{label}: segments overlap or are unordered")
        start_bin = round(start / BIN_MILES)
        end_bin = round(end / BIN_MILES)
        if not math.isclose(
            start, start_bin * BIN_MILES, abs_tol=1e-8
        ) or not math.isclose(end, end_bin * BIN_MILES, abs_tol=1e-8):
            raise CoverageInvariantError(
                f"{label}: segment endpoints must align to coverage bins"
            )
        expected_miles = (end_bin - start_bin) * BIN_MILES
        if not math.isclose(miles, expected_miles, abs_tol=1e-8):
            raise CoverageInvariantError(
                f"{label}: segment mileage does not match its endpoints"
            )
        result.update(range(start_bin, end_bin))
        previous_end = end
    return result


def _require_close(actual: Any, expected: float, label: str, tolerance: float) -> None:
    try:
        numeric = float(actual)
    except (TypeError, ValueError) as error:
        raise CoverageInvariantError(f"{label}: expected a number") from error
    if not math.isclose(numeric, expected, abs_tol=tolerance):
        raise CoverageInvariantError(f"{label}: expected {expected}, found {numeric}")


def validate_coverage_outputs(
    records: list[dict[str, Any]],
    coverage: dict[str, Any],
) -> None:
    """Validate bin, chronology, partition, and activity-union invariants."""
    running_bins: set[int] = set()
    type_bins: dict[str, set[int]] = {"run": set(), "bike": set(), "hike": set()}
    previous_cumulative = 0.0
    summed_new = 0.0
    mile_tolerance = BIN_MILES / 2 + 1e-9
    chronology: list[tuple[str, int]] = []

    for record in records:
        activity_id = record.get("activity_id", "unknown")
        label = f"activity {activity_id}"
        bins = _serialized_segment_bins(record.get("segments"), label)
        if not bins:
            raise CoverageInvariantError(f"{label}: covered activity has no bins")
        new_bins = bins - running_bins
        activity_type = record.get("activity_type")
        if activity_type not in type_bins:
            raise CoverageInvariantError(f"{label}: unsupported activity type")
        type_bins[activity_type].update(bins)

        unique_miles = float(record.get("co_unique_miles", -1))
        new_miles = float(record.get("new_co_unique_miles", -1))
        cumulative_miles = float(record.get("cumulative_unique_miles", -1))
        if new_miles < 0:
            raise CoverageInvariantError(f"{label}: new coverage is negative")
        if new_miles > unique_miles + mile_tolerance:
            raise CoverageInvariantError(
                f"{label}: new coverage exceeds activity unique coverage"
            )
        _require_close(
            unique_miles,
            len(bins) * BIN_MILES,
            f"{label} unique coverage",
            mile_tolerance,
        )
        _require_close(
            new_miles,
            len(new_bins) * BIN_MILES,
            f"{label} new coverage",
            mile_tolerance,
        )

        running_bins.update(bins)
        expected_cumulative = len(running_bins) * BIN_MILES
        if cumulative_miles + mile_tolerance < previous_cumulative:
            raise CoverageInvariantError(f"{label}: cumulative coverage decreased")
        _require_close(
            cumulative_miles,
            expected_cumulative,
            f"{label} cumulative coverage",
            mile_tolerance,
        )
        percent = float(record.get("cumulative_percent_complete", -1))
        if not 0 <= percent <= 100 + 1e-9:
            raise CoverageInvariantError(
                f"{label}: cumulative percent is outside 0-100"
            )
        _require_close(
            percent,
            expected_cumulative / CANAL_MILES * 100,
            f"{label} cumulative percent",
            0.0005 + 1e-9,
        )
        minimum = float(record.get("min_milepost", -1))
        maximum = float(record.get("max_milepost", -1))
        if not 0 <= minimum <= maximum <= CANAL_MILES:
            raise CoverageInvariantError(f"{label}: invalid min/max milepost")
        segments = record["segments"]
        if not math.isclose(
            minimum, float(segments[0]["start_milepost"])
        ) or not math.isclose(maximum, float(segments[-1]["end_milepost"])):
            raise CoverageInvariantError(
                f"{label}: min/max milepost does not bound its segments"
            )
        summed_new += new_miles
        previous_cumulative = cumulative_miles
        chronology.append((str(record.get("datetime", "")), int(activity_id)))

    if chronology != sorted(chronology):
        raise CoverageInvariantError(
            "activity records are not in deterministic chronology"
        )

    combined_miles = len(running_bins) * BIN_MILES
    final_cumulative = float(records[-1]["cumulative_unique_miles"]) if records else 0.0
    _require_close(
        final_cumulative,
        combined_miles,
        "final cumulative coverage versus activity-bin union",
        mile_tolerance,
    )
    _require_close(
        summed_new,
        final_cumulative,
        "sum of new coverage versus final cumulative coverage",
        mile_tolerance,
    )
    _require_close(
        coverage.get("combined_unique_miles"),
        combined_miles,
        "coverage summary combined mileage",
        mile_tolerance,
    )
    summary_percent = float(coverage.get("combined_percent_complete", -1))
    if not 0 <= summary_percent <= 100 + 1e-9:
        raise CoverageInvariantError("summary completion percent is outside 0-100")
    _require_close(
        summary_percent,
        combined_miles / CANAL_MILES * 100,
        "coverage summary completion percent",
        0.0005 + 1e-9,
    )
    _require_close(
        coverage.get("remaining_miles"),
        CANAL_MILES - combined_miles,
        "coverage summary remaining mileage",
        mile_tolerance,
    )

    completed_bins = _serialized_segment_bins(
        coverage.get("completed_segments"), "completed coverage"
    )
    remaining_bins = _serialized_segment_bins(
        coverage.get("remaining_segments"), "remaining coverage"
    )
    if completed_bins & remaining_bins:
        raise CoverageInvariantError("completed and remaining coverage overlap")
    if completed_bins != running_bins:
        raise CoverageInvariantError(
            "completed coverage does not equal the union of activity bins"
        )
    if completed_bins | remaining_bins != set(range(BIN_COUNT)):
        raise CoverageInvariantError(
            "completed and remaining coverage do not partition the full canal"
        )
    longest = coverage.get("longest_continuous_completed_section")
    if longest is not None:
        longest_bins = _serialized_segment_bins([longest], "longest completed section")
        if not longest_bins <= completed_bins:
            raise CoverageInvariantError(
                "longest completed section is outside completed coverage"
            )

    for activity_type, bins in type_bins.items():
        if not bins <= running_bins:
            raise CoverageInvariantError(
                f"{activity_type} coverage is not a subset of combined coverage"
            )
        _require_close(
            coverage.get(f"{activity_type}_unique_miles"),
            len(bins) * BIN_MILES,
            f"{activity_type} coverage summary",
            mile_tolerance,
        )


def audit_activity_record(record: dict[str, Any]) -> tuple[str, ...]:
    """Return non-mutating review flags for one already-matched activity."""
    reasons: list[str] = []
    match_percentage = float(record.get("match_percentage", 0))
    matched_points = int(record.get("matched_point_count", 0))
    co_travel = float(record.get("co_travel_miles", 0))
    strava_distance = record.get("strava_distance_miles")
    if match_percentage < LOW_MATCH_PERCENTAGE:
        reasons.append("LOW_MATCH_PERCENTAGE")
    if matched_points < LOW_MATCHED_POINT_COUNT:
        reasons.append("LOW_MATCHED_POINT_COUNT")
    if strava_distance is not None:
        strava_miles = float(strava_distance)
        excess = co_travel - strava_miles
        if excess > TRAVEL_EXCEEDS_STRAVA_TOLERANCE_MILES:
            reasons.append("CO_TRAVEL_EXCEEDS_STRAVA")
        if excess > 0 and (
            strava_miles <= 0 or excess / strava_miles >= LARGE_TRAVEL_EXCESS_RATIO
        ):
            reasons.append("LARGE_CO_TRAVEL_DISCREPANCY")
    segments = record.get("segments", [])
    if len(segments) > 1:
        reasons.append("DISCONNECTED_MATCH")
    diagnostics = record.get("matching_diagnostics") or {}
    median_offset = diagnostics.get("median_distance_meters")
    overlaps_parallel_review = any(
        float(segment["start_milepost"]) < interval_end
        and float(segment["end_milepost"]) > interval_start
        for segment in segments
        for interval_start, interval_end in PARALLEL_REVIEW_INTERVALS
    )
    if (
        median_offset is not None
        and float(median_offset) >= PARALLEL_MIN_MEDIAN_OFFSET_METERS
        and float(record.get("co_unique_miles", 0)) >= PARALLEL_MIN_UNIQUE_MILES
        and overlaps_parallel_review
    ):
        reasons.append("POSSIBLE_PARALLEL_TRAIL")
    if record.get("diagnostics"):
        reasons.append("OTHER_EXISTING_DIAGNOSTIC")
    return tuple(reasons)


def calculate_history(
    matched: list[tuple[Activity, ActivityMatch]],
    *,
    reference_sha256: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    chronological = sorted(
        matched, key=lambda item: (item[0].activity_datetime, item[0].activity_id)
    )
    combined_bins: set[int] = set()
    type_bins: dict[str, set[int]] = {"run": set(), "bike": set(), "hike": set()}
    records: list[dict[str, Any]] = []
    for activity, match in chronological:
        new_bins = set(match.covered_bins) - combined_bins
        combined_bins.update(match.covered_bins)
        type_bins[activity.normalized_activity_type].update(match.covered_bins)
        records.append(_activity_record(activity, match, new_bins, combined_bins))

    completed = bins_to_segments(combined_bins)
    remaining_bins = set(range(BIN_COUNT)) - combined_bins
    remaining = bins_to_segments(remaining_bins)
    longest = max(completed, key=lambda segment: segment.miles, default=None)
    combined_miles = len(combined_bins) * BIN_MILES
    coverage = {
        "canal_miles": CANAL_MILES,
        "bin_miles": BIN_MILES,
        "methodology": {
            "match_tolerance_meters": MATCH_TOLERANCE_METERS,
            "projected_crs": MATCH_CRS,
            "canonical_towpath_sha256": reference_sha256,
            "continuity": {
                "max_source_gap_meters": MAX_SOURCE_GAP_METERS,
                "max_along_gap_meters": MAX_ALONG_GAP_METERS,
                "max_time_gap_seconds": MAX_TIME_GAP_SECONDS,
                "minimum_alignment_ratio": MINIMUM_ALIGNMENT_RATIO,
                "minimum_alignment_distance_meters": (
                    MINIMUM_ALIGNMENT_DISTANCE_METERS
                ),
                "minimum_run_points": MINIMUM_RUN_POINTS,
                "minimum_run_travel_meters": MINIMUM_RUN_TRAVEL_METERS,
            },
            "audit_thresholds": {
                "low_match_percentage": LOW_MATCH_PERCENTAGE,
                "low_matched_point_count": LOW_MATCHED_POINT_COUNT,
                "travel_exceeds_strava_tolerance_miles": (
                    TRAVEL_EXCEEDS_STRAVA_TOLERANCE_MILES
                ),
                "large_travel_excess_ratio": LARGE_TRAVEL_EXCESS_RATIO,
                "parallel_min_median_offset_meters": (
                    PARALLEL_MIN_MEDIAN_OFFSET_METERS
                ),
                "parallel_min_unique_miles": PARALLEL_MIN_UNIQUE_MILES,
                "parallel_review_intervals": [
                    {"start_milepost": start, "end_milepost": end}
                    for start, end in PARALLEL_REVIEW_INTERVALS
                ],
            },
        },
        "combined_unique_miles": round(combined_miles, 2),
        "combined_percent_complete": round(combined_miles / CANAL_MILES * 100, 3),
        "remaining_miles": round(CANAL_MILES - combined_miles, 2),
        "run_unique_miles": round(len(type_bins["run"]) * BIN_MILES, 2),
        "bike_unique_miles": round(len(type_bins["bike"]) * BIN_MILES, 2),
        "hike_unique_miles": round(len(type_bins["hike"]) * BIN_MILES, 2),
        "activity_count": len(records),
        "first_activity_date": records[0]["date"] if records else None,
        "latest_activity_date": records[-1]["date"] if records else None,
        "longest_continuous_completed_section": (
            longest.as_dict() if longest else None
        ),
        "completed_segments": [segment.as_dict() for segment in completed],
        "remaining_segments": [segment.as_dict() for segment in remaining],
        "run_segments": [
            segment.as_dict() for segment in bins_to_segments(type_bins["run"])
        ],
        "bike_segments": [
            segment.as_dict() for segment in bins_to_segments(type_bins["bike"])
        ],
        "hike_segments": [
            segment.as_dict() for segment in bins_to_segments(type_bins["hike"])
        ],
    }
    validate_coverage_outputs(records, coverage)
    return records, coverage


__all__ = [
    "BIN_MILES",
    "CANAL_MILES",
    "MATCH_CRS",
    "MATCH_TOLERANCE_METERS",
    "ActivityMatch",
    "CoverageEngine",
    "CoverageError",
    "CoverageInvariantError",
    "CoverageSegment",
    "audit_activity_record",
    "bins_to_segments",
    "calculate_history",
    "canonical_reference_sha256",
    "milepost_intervals_to_geometries",
    "validate_coverage_outputs",
]
