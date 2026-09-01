"""Export public Canawler JSON artifacts as tidy analytics CSV tables."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DIR = PROJECT_ROOT / "data" / "public"

MODES = ("run", "bike", "hike")
AMENITIES = (
    "bike_rentals",
    "boat_ramp",
    "boat_rentals",
    "camping",
    "camping_fee_area",
    "camping_tent",
    "canal_quarters",
    "canoe_kayak_ramp",
    "fee_area",
    "food",
    "parking",
    "picnic_tables",
    "restrooms",
    "visitor_center",
    "water",
)

FEATURE_SCHEMA = {
    "id": pl.String,
    "feature_type": pl.String,
    "name": pl.String,
    "common_name": pl.String,
    "lock_number": pl.String,
    "milepost": pl.Float64,
    "milepost_end": pl.Float64,
    "latitude": pl.Float64,
    "longitude": pl.Float64,
    "coverage_status": pl.String,
    "run_coverage_status": pl.String,
    "bike_coverage_status": pl.String,
    "hike_coverage_status": pl.String,
    "covering_activity_count": pl.Int64,
    "run_covering_activity_count": pl.Int64,
    "bike_covering_activity_count": pl.Int64,
    "hike_covering_activity_count": pl.Int64,
    "first_covering_activity_id": pl.Int64,
    "first_covering_date": pl.String,
    "first_covering_strava_url": pl.String,
    "latest_covering_activity_id": pl.Int64,
    "latest_covering_date": pl.String,
    "latest_covering_strava_url": pl.String,
    "remaining_segment_count": pl.Int64,
    "remaining_segment_start": pl.Float64,
    "remaining_segment_end": pl.Float64,
    "remaining_segment_miles": pl.Float64,
    "nps_has_amenity_data": pl.Boolean,
    **{f"nps_{amenity}": pl.Boolean for amenity in AMENITIES},
    "nps_recreation_match_count": pl.Int64,
    "nearby_feature_count": pl.Int64,
    "nearby_lock_count": pl.Int64,
}

NEARBY_FEATURE_SCHEMA = {
    "access_point_id": pl.String,
    "access_point_name": pl.String,
    "access_point_milepost": pl.Float64,
    "nearby_feature_id": pl.String,
    "nearby_feature_type": pl.String,
    "nearby_feature_name": pl.String,
    "nearby_feature_common_name": pl.String,
    "nearby_feature_lock_number": pl.String,
    "nearby_feature_milepost": pl.Float64,
    "milepost_distance": pl.Float64,
    "source": pl.String,
}

NPS_MATCH_SCHEMA = {
    "access_point_id": pl.String,
    "access_point_name": pl.String,
    "access_point_milepost": pl.Float64,
    "access_point_milepost_end": pl.Float64,
    "nps_location_name": pl.String,
    "nps_milepost": pl.Float64,
    "milepost_distance": pl.Float64,
}

COVERAGE_SEGMENT_SCHEMA = {
    "coverage_scope": pl.String,
    "coverage_status": pl.String,
    "start_milepost": pl.Float64,
    "end_milepost": pl.Float64,
    "miles": pl.Float64,
}


class ExportError(RuntimeError):
    """Raised when a public JSON input violates the export contract."""


def _relative(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def _load_json(filename: str) -> dict[str, Any]:
    path = PUBLIC_DIR / filename
    if not path.is_file():
        raise ExportError(
            f"Missing {_relative(path)}. Run `uv run canawler build` first."
        )

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ExportError(f"Expected an object at the root of {_relative(path)}.")
    return data


def _required_list(root: dict[str, Any], key: str, filename: str) -> list[Any]:
    value = root.get(key)
    if not isinstance(value, list):
        raise ExportError(f"Expected `{key}` to be an array in data/public/{filename}.")
    return value


def _validate_unique_ids(records: list[dict[str, Any]], label: str) -> set[str]:
    ids: list[str] = []
    for record in records:
        record_id = record.get("id")
        if not isinstance(record_id, str) or not record_id:
            raise ExportError(f"Every {label} record must have a non-empty string ID.")
        ids.append(record_id)
    if len(ids) != len(set(ids)):
        raise ExportError(f"Duplicate {label} IDs found.")
    return set(ids)


def _validate_modes(record: dict[str, Any], feature_id: str) -> None:
    for key in ("coverage_by_mode", "covering_activity_count_by_mode"):
        value = record.get(key)
        if not isinstance(value, dict) or set(value) != set(MODES):
            raise ExportError(f"{feature_id} has an invalid `{key}` object.")


def _activity_fields(activity: Any, *, prefix: str, feature_id: str) -> dict[str, Any]:
    columns = {
        f"{prefix}_covering_activity_id": None,
        f"{prefix}_covering_date": None,
        f"{prefix}_covering_strava_url": None,
    }
    if activity is None:
        return columns
    if not isinstance(activity, dict) or set(activity) != {
        "activity_id",
        "date",
        "strava_url",
    }:
        raise ExportError(
            f"{feature_id} has an invalid `{prefix}_covering_activity` object."
        )
    columns.update(
        {
            f"{prefix}_covering_activity_id": activity["activity_id"],
            f"{prefix}_covering_date": activity["date"],
            f"{prefix}_covering_strava_url": activity["strava_url"],
        }
    )
    return columns


def _remaining_segment_fields(segments: Any, feature_id: str) -> dict[str, Any]:
    if not isinstance(segments, list):
        raise ExportError(f"{feature_id} has invalid `remaining_segments`.")
    fields = {
        "remaining_segment_count": len(segments),
        "remaining_segment_start": None,
        "remaining_segment_end": None,
        "remaining_segment_miles": None,
    }
    if len(segments) == 1:
        segment = segments[0]
        if not isinstance(segment, dict):
            raise ExportError(f"{feature_id} has an invalid remaining segment.")
        fields.update(
            {
                "remaining_segment_start": segment.get("start_milepost"),
                "remaining_segment_end": segment.get("end_milepost"),
                "remaining_segment_miles": segment.get("miles"),
            }
        )
    return fields


def _coverage_fields(record: dict[str, Any], feature_id: str) -> dict[str, Any]:
    _validate_modes(record, feature_id)
    coverage_by_mode = record["coverage_by_mode"]
    count_by_mode = record["covering_activity_count_by_mode"]
    fields: dict[str, Any] = {
        "coverage_status": record.get("coverage_status"),
        "covering_activity_count": record.get("covering_activity_count"),
    }
    for mode in MODES:
        fields[f"{mode}_coverage_status"] = coverage_by_mode[mode]
        fields[f"{mode}_covering_activity_count"] = count_by_mode[mode]
    fields.update(
        _activity_fields(
            record.get("first_covering_activity"),
            prefix="first",
            feature_id=feature_id,
        )
    )
    fields.update(
        _activity_fields(
            record.get("latest_covering_activity"),
            prefix="latest",
            feature_id=feature_id,
        )
    )
    fields.update(
        _remaining_segment_fields(record.get("remaining_segments"), feature_id)
    )
    return fields


def _empty_amenity_fields() -> dict[str, Any]:
    return {
        "nps_has_amenity_data": None,
        **{f"nps_{amenity}": None for amenity in AMENITIES},
    }


def _access_point_amenity_fields(record: dict[str, Any]) -> dict[str, Any]:
    amenities = record.get("nps_amenities")
    if amenities is None:
        return {
            "nps_has_amenity_data": False,
            **{f"nps_{amenity}": None for amenity in AMENITIES},
        }
    if not isinstance(amenities, dict) or set(amenities) != set(AMENITIES):
        raise ExportError(f"{record['id']} has an invalid `nps_amenities` object.")
    return {
        "nps_has_amenity_data": True,
        **{f"nps_{amenity}": amenities[amenity] for amenity in AMENITIES},
    }


def _feature_frame(
    locks: list[dict[str, Any]], access_points: list[dict[str, Any]]
) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for lock in locks:
        feature_id = lock["id"]
        row = {
            "id": feature_id,
            "feature_type": "lock",
            "name": lock.get("name"),
            "common_name": lock.get("common_name"),
            "lock_number": lock.get("lock_number"),
            "milepost": lock.get("milepost"),
            "milepost_end": None,
            "latitude": None,
            "longitude": None,
            **_coverage_fields(lock, feature_id),
            **_empty_amenity_fields(),
            "nps_recreation_match_count": None,
            "nearby_feature_count": None,
            "nearby_lock_count": None,
        }
        rows.append(row)

    for access_point in access_points:
        feature_id = access_point["id"]
        matches = access_point.get("nps_recreation_matches")
        nearby_features = access_point.get("nearby_features")
        if not isinstance(matches, list) or not isinstance(nearby_features, list):
            raise ExportError(f"{feature_id} has invalid relationship arrays.")
        row = {
            "id": feature_id,
            "feature_type": "access_point",
            "name": access_point.get("name"),
            "common_name": None,
            "lock_number": None,
            "milepost": access_point.get("milepost"),
            "milepost_end": access_point.get("milepost_end"),
            "latitude": access_point.get("latitude"),
            "longitude": access_point.get("longitude"),
            **_coverage_fields(access_point, feature_id),
            **_access_point_amenity_fields(access_point),
            "nps_recreation_match_count": len(matches),
            "nearby_feature_count": len(nearby_features),
            "nearby_lock_count": sum(
                feature.get("type") == "lock" and feature.get("source") == "locks"
                for feature in nearby_features
            ),
        }
        rows.append(row)

    frame = pl.DataFrame(rows, schema=FEATURE_SCHEMA)
    expected_count = len(locks) + len(access_points)
    if frame.height != expected_count or frame["id"].n_unique() != expected_count:
        raise ExportError("Feature export contains missing or duplicate rows.")
    required_columns = ("id", "feature_type", "name", "milepost", "coverage_status")
    if any(frame[column].null_count() for column in required_columns):
        raise ExportError("Feature export contains null required values.")
    if not set(frame["feature_type"].to_list()) <= {"lock", "access_point"}:
        raise ExportError("Feature export contains an invalid feature type.")
    return frame.sort(["milepost", "feature_type", "name", "id"])


def _nearby_feature_frame(
    access_points: list[dict[str, Any]],
    access_point_ids: set[str],
    feature_ids: set[str],
    lock_ids: set[str],
) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for access_point in access_points:
        access_point_id = access_point["id"]
        if access_point_id not in access_point_ids:
            raise ExportError(f"Unknown access-point ID {access_point_id}.")
        for feature in access_point["nearby_features"]:
            feature_id = feature.get("id")
            is_canonical_lock = (
                feature.get("type") == "lock" and feature.get("source") == "locks"
            )
            if is_canonical_lock and feature_id not in lock_ids:
                raise ExportError(
                    f"Nearby lock ID {feature_id!r} does not resolve to a public lock."
                )
            if feature_id is not None and feature_id not in feature_ids:
                raise ExportError(
                    f"Nearby feature ID {feature_id!r} does not resolve to a feature."
                )
            rows.append(
                {
                    "access_point_id": access_point_id,
                    "access_point_name": access_point["name"],
                    "access_point_milepost": access_point["milepost"],
                    "nearby_feature_id": feature_id,
                    "nearby_feature_type": feature.get("type"),
                    "nearby_feature_name": feature.get("name"),
                    "nearby_feature_common_name": feature.get("common_name"),
                    "nearby_feature_lock_number": feature.get("lock_number"),
                    "nearby_feature_milepost": feature.get("milepost"),
                    "milepost_distance": feature.get("milepost_distance"),
                    "source": feature.get("source"),
                }
            )
    frame = pl.DataFrame(rows, schema=NEARBY_FEATURE_SCHEMA)
    return frame.sort(
        [
            "access_point_milepost",
            "access_point_id",
            "milepost_distance",
            "nearby_feature_milepost",
            "nearby_feature_type",
            "nearby_feature_name",
        ]
    )


def _nps_match_frame(
    access_points: list[dict[str, Any]], access_point_ids: set[str]
) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for access_point in access_points:
        access_point_id = access_point["id"]
        if access_point_id not in access_point_ids:
            raise ExportError(f"Unknown access-point ID {access_point_id}.")
        for match in access_point["nps_recreation_matches"]:
            distance = match.get("milepost_distance")
            if isinstance(distance, bool) or not isinstance(distance, (int, float)):
                raise ExportError(
                    f"{access_point_id} has a non-numeric NPS match distance."
                )
            rows.append(
                {
                    "access_point_id": access_point_id,
                    "access_point_name": access_point["name"],
                    "access_point_milepost": access_point["milepost"],
                    "access_point_milepost_end": access_point.get("milepost_end"),
                    "nps_location_name": match.get("name"),
                    "nps_milepost": match.get("milepost"),
                    "milepost_distance": distance,
                }
            )
    frame = pl.DataFrame(rows, schema=NPS_MATCH_SCHEMA)
    return frame.sort(
        [
            "access_point_milepost",
            "access_point_id",
            "milepost_distance",
            "nps_milepost",
            "nps_location_name",
        ]
    )


def _coverage_segment_frame(coverage: dict[str, Any]) -> pl.DataFrame:
    definitions = (
        ("completed_segments", "combined", "completed", 0, 0),
        ("remaining_segments", "combined", "remaining", 0, 1),
        ("run_segments", "run", "covered", 1, 0),
        ("bike_segments", "bike", "covered", 2, 0),
        ("hike_segments", "hike", "covered", 3, 0),
    )
    rows: list[dict[str, Any]] = []
    for key, scope, status, scope_order, status_order in definitions:
        for segment in _required_list(coverage, key, "coverage.json"):
            if not isinstance(segment, dict):
                raise ExportError(
                    f"Expected objects in `{key}` in data/public/coverage.json."
                )
            rows.append(
                {
                    "coverage_scope": scope,
                    "coverage_status": status,
                    "start_milepost": segment.get("start_milepost"),
                    "end_milepost": segment.get("end_milepost"),
                    "miles": segment.get("miles"),
                    "_scope_order": scope_order,
                    "_status_order": status_order,
                }
            )
    frame = pl.DataFrame(
        rows,
        schema={
            **COVERAGE_SEGMENT_SCHEMA,
            "_scope_order": pl.Int8,
            "_status_order": pl.Int8,
        },
    )
    return frame.sort(
        ["_scope_order", "_status_order", "start_milepost", "end_milepost"]
    ).select(COVERAGE_SEGMENT_SCHEMA.keys())


def _write_csv(frame: pl.DataFrame, filename: str) -> tuple[Path, bool]:
    path = PUBLIC_DIR / filename
    contents = frame.write_csv(quote_style="non_numeric")
    if not contents.endswith("\n"):
        contents += "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == contents:
        return path, False
    path.write_text(contents, encoding="utf-8")
    return path, True


def main() -> None:
    locks_root = _load_json("locks.json")
    access_points_root = _load_json("access-points.json")
    coverage = _load_json("coverage.json")

    locks = _required_list(locks_root, "locks", "locks.json")
    access_points = _required_list(
        access_points_root, "access_points", "access-points.json"
    )
    if not all(isinstance(record, dict) for record in [*locks, *access_points]):
        raise ExportError("Feature arrays must contain objects.")

    lock_ids = _validate_unique_ids(locks, "lock")
    access_point_ids = _validate_unique_ids(access_points, "access-point")
    collisions = lock_ids & access_point_ids
    if collisions:
        raise ExportError(f"Lock and access-point IDs collide: {sorted(collisions)}")
    feature_ids = lock_ids | access_point_ids

    features = _feature_frame(locks, access_points)
    nearby_features = _nearby_feature_frame(
        access_points, access_point_ids, feature_ids, lock_ids
    )
    nps_matches = _nps_match_frame(access_points, access_point_ids)
    coverage_segments = _coverage_segment_frame(coverage)

    outputs = (
        (features, "features.csv"),
        (nearby_features, "feature-nearby-features.csv"),
        (nps_matches, "access-point-nps-matches.csv"),
        (coverage_segments, "coverage-segments.csv"),
    )
    results = [_write_csv(frame, filename) for frame, filename in outputs]

    print("CSV analytics export")
    print(f"Features: {features.height}")
    print(f"Nearby feature relationships: {nearby_features.height}")
    print(f"NPS matches: {nps_matches.height}")
    print(f"Coverage segments: {coverage_segments.height}")
    print()
    for path, changed in results:
        print(f"{'Wrote' if changed else 'Unchanged'} {_relative(path)}")


if __name__ == "__main__":
    try:
        main()
    except ExportError as error:
        raise SystemExit(str(error)) from None
