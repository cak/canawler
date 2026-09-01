"""Validate local visitor references and derive frontend-ready artifacts."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from canawler.coverage import (
    BIN_COUNT,
    BIN_MILES,
    CANAL_MILES,
    _serialized_segment_bins,
)
from canawler.reference import ReferenceDataError

ACCESS_POINTS_PATH = Path("data/reference/access-points.json")
RECREATION_GUIDE_PATH = Path("data/reference/recreation-guide.json")
LOCKS_PATH = Path("data/reference/locks.json")
ACTIVITY_MODES = ("run", "bike", "hike")
PUBLIC_SCHEMA_VERSION = 1

AMENITY_FIELDS = (
    "parking",
    "restrooms",
    "water",
    "picnic_tables",
    "camping",
    "camping_fee_area",
    "camping_tent",
    "boat_ramp",
    "canoe_kayak_ramp",
    "visitor_center",
    "food",
    "bike_rentals",
    "boat_rentals",
    "canal_quarters",
    "fee_area",
)

DERIVED_METADATA = {
    "coverage": "Canawler calculated C&O towpath coverage using 0.01-mile bins",
    "activity_history": (
        "Canawler qualifying activities whose calculated C&O coverage intersects "
        "the feature's 0.01-mile bin or bins"
    ),
}


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ReferenceDataError(
            f"could not read reference data {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise ReferenceDataError(f"{path}: root must be an object")
    return value


def _valid_source(source: Any, label: str) -> None:
    if not isinstance(source, dict):
        raise ReferenceDataError(f"{label}: source metadata must be an object")
    if not str(source.get("name", "")).strip():
        raise ReferenceDataError(f"{label}: source name must be non-empty")
    if not str(source.get("url", "")).strip():
        raise ReferenceDataError(f"{label}: source URL must be non-empty")


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReferenceDataError(f"{label}: expected a number")
    return float(value)


def load_access_points(path: Path = ACCESS_POINTS_PATH) -> dict[str, Any]:
    data = _load_object(path)
    _valid_source(data.get("source"), str(path))
    records = data.get("access_points")
    if not isinstance(records, list):
        raise ReferenceDataError(f"{path}: access_points must be an array")

    previous: tuple[float, str] | None = None
    seen: set[tuple[Any, ...]] = set()
    expected_fields = {"name", "milepost", "milepost_end", "latitude", "longitude"}
    for index, record in enumerate(records):
        label = f"{path}: access point {index}"
        if not isinstance(record, dict) or set(record) != expected_fields:
            raise ReferenceDataError(
                f"{label}: fields do not match the reference schema"
            )
        name = record.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ReferenceDataError(f"{label}: name must be non-empty")
        milepost = _number(record.get("milepost"), f"{label} milepost")
        if not 0 <= milepost <= CANAL_MILES:
            raise ReferenceDataError(f"{label}: milepost is outside the canal")
        end_value = record.get("milepost_end")
        if end_value is not None:
            end = _number(end_value, f"{label} milepost_end")
            if not milepost <= end <= CANAL_MILES:
                raise ReferenceDataError(f"{label}: milepost_end is invalid")
        latitude = _number(record.get("latitude"), f"{label} latitude")
        longitude = _number(record.get("longitude"), f"{label} longitude")
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ReferenceDataError(f"{label}: coordinates are invalid")
        order = (milepost, name)
        if previous is not None and order < previous:
            raise ReferenceDataError(f"{path}: access points are not sorted")
        previous = order
        duplicate = (name, milepost, end_value, latitude, longitude)
        if duplicate in seen:
            raise ReferenceDataError(f"{label}: exact duplicate")
        seen.add(duplicate)
    return data


def _normalize_name(value: str) -> str:
    value = value.casefold().replace("’", "'")
    value = re.sub(r"'s\b", "s", value)
    value = re.sub(r"\bno\.?\s+(?=\d)", "", value)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value).split())


def _id_part(value: Any) -> str:
    ascii_value = (
        unicodedata.normalize("NFKD", str(value))
        .encode("ascii", "ignore")
        .decode("ascii")
        .casefold()
    )
    return "-".join(re.findall(r"[a-z0-9]+", ascii_value))


def _number_id_part(value: Any) -> str:
    return _id_part(str(value).replace("-", "minus-"))


def _lock_id(lock_number: str) -> str:
    return f"lock-{_id_part(lock_number)}"


def _access_point_ids(access_points: list[dict[str, Any]]) -> list[str]:
    bases = [
        "-".join(
            part
            for part in (
                _id_part(access_point["name"]),
                _number_id_part(access_point["milepost"]),
                (
                    _number_id_part(access_point["milepost_end"])
                    if access_point["milepost_end"] is not None
                    else ""
                ),
            )
            if part
        )
        for access_point in access_points
    ]
    base_counts = Counter(bases)
    candidates = []
    for base, access_point in zip(bases, access_points, strict=True):
        if base_counts[base] == 1:
            candidates.append(base)
            continue
        candidates.append(
            f"{base}-lat-{_number_id_part(access_point['latitude'])}"
            f"-lon-{_number_id_part(access_point['longitude'])}"
        )

    candidate_counts = Counter(candidates)
    result = []
    for candidate, access_point in zip(candidates, access_points, strict=True):
        if candidate_counts[candidate] == 1:
            result.append(candidate)
            continue
        canonical = json.dumps(
            access_point, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:8]
        result.append(f"{candidate}-{digest}")
    if len(set(result)) != len(result) or any(
        re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) is None for value in result
    ):
        raise ReferenceDataError("access-point public IDs are not unique and URL-safe")
    return result


def load_recreation_guide(path: Path = RECREATION_GUIDE_PATH) -> dict[str, Any]:
    data = _load_object(path)
    _valid_source(data.get("source"), str(path))
    records = data.get("locations")
    if not isinstance(records, list):
        raise ReferenceDataError(f"{path}: locations must be an array")

    previous: tuple[float, str] | None = None
    seen: set[tuple[Any, ...]] = set()
    for index, record in enumerate(records):
        label = f"{path}: location {index}"
        if not isinstance(record, dict):
            raise ReferenceDataError(f"{label}: must be an object")
        name = record.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ReferenceDataError(f"{label}: name must be non-empty")
        milepost = _number(record.get("milepost"), f"{label} milepost")
        if not 0 <= milepost <= CANAL_MILES:
            raise ReferenceDataError(f"{label}: milepost is outside the canal")
        amenities = record.get("amenities")
        if not isinstance(amenities, dict) or tuple(sorted(amenities)) != tuple(
            sorted(AMENITY_FIELDS)
        ):
            raise ReferenceDataError(f"{label}: amenities do not match the schema")
        if any(not isinstance(amenities[field], bool) for field in AMENITY_FIELDS):
            raise ReferenceDataError(f"{label}: amenities must be booleans")
        if amenities["camping"] != (
            amenities["camping_fee_area"] or amenities["camping_tent"]
        ):
            raise ReferenceDataError(
                f"{label}: camping must combine the camping fields"
            )
        order = (milepost, name)
        if previous is not None and order < previous:
            raise ReferenceDataError(f"{path}: locations are not sorted")
        previous = order
        normalized = (
            _normalize_name(name),
            milepost,
            *(amenities[field] for field in AMENITY_FIELDS),
        )
        if normalized in seen:
            raise ReferenceDataError(f"{label}: exact normalized duplicate")
        seen.add(normalized)
    return data


def load_locks(path: Path = LOCKS_PATH) -> dict[str, Any]:
    data = _load_object(path)
    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ReferenceDataError(f"{path}: sources must be a non-empty array")
    for source in sources:
        _valid_source(source, str(path))
    records = data.get("locks")
    if not isinstance(records, list) or len(records) != 74:
        raise ReferenceDataError(f"{path}: expected exactly 74 locks")

    previous: tuple[float, str] | None = None
    numbers: set[str] = set()
    for index, record in enumerate(records):
        label = f"{path}: lock {index}"
        if not isinstance(record, dict):
            raise ReferenceDataError(f"{label}: must be an object")
        number = record.get("lock_number")
        name = record.get("name")
        if not isinstance(number, str) or not number.strip():
            raise ReferenceDataError(f"{label}: lock_number must be a non-empty string")
        if number in numbers:
            raise ReferenceDataError(f"{label}: duplicate lock_number")
        numbers.add(number)
        if not isinstance(name, str) or not name.strip():
            raise ReferenceDataError(f"{label}: name must be non-empty")
        milepost = _number(record.get("milepost"), f"{label} milepost")
        alternate = record.get("alternate_milepost")
        if not 0 <= milepost <= CANAL_MILES:
            raise ReferenceDataError(f"{label}: milepost is outside the canal")
        if (
            alternate is not None
            and not 0
            <= _number(alternate, f"{label} alternate_milepost")
            <= CANAL_MILES
        ):
            raise ReferenceDataError(
                f"{label}: alternate_milepost is outside the canal"
            )
        order = (milepost, number)
        if previous is not None and order < previous:
            raise ReferenceDataError(f"{path}: locks are not sorted")
        previous = order
    required = {"1", "75", "63 1/3", "64 2/3"}
    if not required <= numbers or "65" in numbers:
        raise ReferenceDataError(f"{path}: required lock numbering invariants failed")
    return data


def _milepost_distance(access_point: dict[str, Any], milepost: float) -> Decimal:
    start = Decimal(str(access_point["milepost"]))
    end = Decimal(str(access_point.get("milepost_end") or access_point["milepost"]))
    feature = Decimal(str(milepost))
    if start <= feature <= end:
        return Decimal(0)
    return min(abs(feature - start), abs(feature - end))


def _names_match(left: str, right: str) -> bool:
    left_name = _normalize_name(left)
    right_name = _normalize_name(right)
    feature_words = ("aqueduct", "bridge", "dam", "lock", "tunnel")
    left_kind = next(
        (word for word in feature_words if word in left_name.split()), None
    )
    right_kind = next(
        (word for word in feature_words if word in right_name.split()), None
    )
    if left_kind is not None and right_kind is not None and left_kind != right_kind:
        return False
    if left_name == right_name or left_name in right_name or right_name in left_name:
        return True
    ignored = {"access", "boat", "center", "lock", "park", "ramp", "state", "visitor"}
    left_tokens = {token for token in left_name.split() if token not in ignored}
    right_tokens = {token for token in right_name.split() if token not in ignored}
    return any(len(token) >= 5 for token in left_tokens & right_tokens)


def _recreation_matches(
    access_points: list[dict[str, Any]], locations: list[dict[str, Any]]
) -> tuple[list[list[int]], set[int]]:
    matches: list[list[int]] = []
    matched_locations: set[int] = set()
    for access_point in access_points:
        indexes = [
            index
            for index, location in enumerate(locations)
            if _milepost_distance(access_point, location["milepost"]) <= Decimal("0.35")
            and _names_match(access_point["name"], location["name"])
        ]
        matches.append(indexes)
        matched_locations.update(indexes)
    return matches, matched_locations


def _access_point_bins(access_point: dict[str, Any]) -> range:
    bin_size = Decimal(str(BIN_MILES))
    first = int(Decimal(str(access_point["milepost"])) // bin_size)
    end = access_point.get("milepost_end")
    last = first if end is None else int(Decimal(str(end)) // bin_size)
    first = min(first, BIN_COUNT - 1)
    last = min(last, BIN_COUNT - 1)
    return range(first, last + 1)


def _coverage_status(access_point: dict[str, Any], covered_bins: set[int]) -> str:
    bins = _access_point_bins(access_point)
    count = sum(index in covered_bins for index in bins)
    if count == len(bins):
        return "covered"
    if access_point.get("milepost_end") is not None and count:
        return "partial"
    return "remaining"


def _activity_coverages(
    activity_records: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], set[int]]]:
    ordered = sorted(
        activity_records,
        key=lambda record: (
            datetime.fromisoformat(str(record["datetime"])),
            str(record.get("activity_id") or ""),
        ),
    )
    return [
        (
            record,
            _serialized_segment_bins(
                record["segments"], f"activity {record.get('activity_id', 'unknown')}"
            ),
        )
        for record in ordered
    ]


def _coverage_event(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "activity_id": record.get("activity_id"),
        "date": record["date"],
        "strava_url": record.get("strava_url"),
    }


def _covering_history(
    feature_bins: range,
    activity_coverages: list[tuple[dict[str, Any], set[int]]],
) -> tuple[
    int,
    dict[str, int],
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    qualifying = []
    seen_activity_ids = set()
    for record, covered_bins in activity_coverages:
        activity_id = record.get("activity_id")
        if activity_id in seen_activity_ids or covered_bins.isdisjoint(feature_bins):
            continue
        qualifying.append(record)
        seen_activity_ids.add(activity_id)
    count_by_mode = {mode: 0 for mode in ACTIVITY_MODES}
    for record in qualifying:
        activity_type = record.get("activity_type")
        if activity_type not in count_by_mode:
            raise ReferenceDataError(
                f"unsupported qualifying activity type: {activity_type!r}"
            )
        count_by_mode[activity_type] += 1
    if len(qualifying) != sum(count_by_mode.values()):
        raise ReferenceDataError("covering activity mode counts do not equal total")
    if not qualifying:
        return 0, count_by_mode, None, None
    return (
        len(qualifying),
        count_by_mode,
        _coverage_event(qualifying[0]),
        _coverage_event(qualifying[-1]),
    )


def _remaining_segments(
    access_point: dict[str, Any], status: str, segments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if status == "covered":
        return []
    start = float(access_point["milepost"])
    end = float(access_point.get("milepost_end") or start)
    return [
        segment
        for segment in segments
        if float(segment["start_milepost"]) <= end
        and (
            float(segment["end_milepost"]) > start
            or float(segment["end_milepost"]) == start == end == CANAL_MILES
        )
    ]


def _lock_number(name: str) -> str | None:
    match = re.search(r"\block\s+(\d+(?:\s+\d+/\d+)?)\b", name, re.IGNORECASE)
    return match.group(1) if match else None


def _feature_type(location: dict[str, Any]) -> str:
    name = location["name"].casefold()
    amenities = location["amenities"]
    if "lockhouse" in name:
        return "lockhouse"
    if "lock" in name:
        return "lock"
    if "aqueduct" in name:
        return "aqueduct"
    if re.search(r"\bdam\b", name):
        return "dam"
    if "tunnel" in name:
        return "tunnel"
    if amenities["visitor_center"] or "visitor center" in name:
        return "visitor_center"
    if amenities["canal_quarters"]:
        return "canal_quarters"
    if amenities["camping"]:
        return "campground"
    if amenities["boat_ramp"] or amenities["canoe_kayak_ramp"]:
        return "boat_access"
    if "fort " in name:
        return "historic_site"
    return "recreation_location"


def build_public_reference_artifacts(
    activity_records: list[dict[str, Any]],
    coverage: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    access_data = load_access_points()
    recreation_data = load_recreation_guide()
    lock_data = load_locks()
    access_points = access_data["access_points"]
    locations = recreation_data["locations"]
    locks = lock_data["locks"]
    access_point_ids = _access_point_ids(access_points)
    lock_ids = {lock["lock_number"]: _lock_id(lock["lock_number"]) for lock in locks}
    if len(set(lock_ids.values())) != len(locks) or any(
        re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) is None
        for value in lock_ids.values()
    ):
        raise ReferenceDataError("lock public IDs are not unique and URL-safe")
    matches, matched_locations = _recreation_matches(access_points, locations)
    covered_bins = _serialized_segment_bins(
        coverage["completed_segments"], "completed coverage"
    )
    activity_coverages = _activity_coverages(activity_records)
    mode_coverage_bins = {
        mode: _serialized_segment_bins(coverage[f"{mode}_segments"], f"{mode} coverage")
        for mode in ACTIVITY_MODES
    }
    remaining = coverage["remaining_segments"]
    canonical_lock_numbers = {record["lock_number"] for record in locks}

    public_access_points = []
    for access_point, access_point_id, match_indexes in zip(
        access_points, access_point_ids, matches, strict=True
    ):
        nps_amenities = (
            {field: False for field in AMENITY_FIELDS} if match_indexes else None
        )
        for index in match_indexes:
            for field in AMENITY_FIELDS:
                nps_amenities[field] = (
                    nps_amenities[field] or locations[index]["amenities"][field]
                )

        nearby_features = []
        for lock in locks:
            distance = _milepost_distance(access_point, lock["milepost"])
            if distance <= Decimal("1.0"):
                nearby_features.append(
                    {
                        "id": lock_ids[lock["lock_number"]],
                        "type": "lock",
                        "lock_number": lock["lock_number"],
                        "name": lock["name"],
                        "common_name": lock["common_name"],
                        "milepost": lock["milepost"],
                        "milepost_distance": float(round(distance, 2)),
                        "source": "locks",
                    }
                )
        for index, location in enumerate(locations):
            if index in matched_locations:
                continue
            number = _lock_number(location["name"])
            if number in canonical_lock_numbers:
                continue
            distance = _milepost_distance(access_point, location["milepost"])
            if distance <= Decimal("1.0"):
                nearby_features.append(
                    {
                        "type": _feature_type(location),
                        "name": location["name"],
                        "milepost": location["milepost"],
                        "milepost_distance": float(round(distance, 2)),
                        "source": "recreation_guide",
                    }
                )
        nearby_features.sort(
            key=lambda feature: (
                feature["milepost_distance"],
                feature["milepost"],
                feature["type"],
                feature["name"],
            )
        )
        status = _coverage_status(access_point, covered_bins)
        coverage_by_mode = {
            mode: _coverage_status(access_point, mode_coverage_bins[mode])
            for mode in ACTIVITY_MODES
        }
        activity_count, count_by_mode, first_activity, latest_activity = (
            _covering_history(_access_point_bins(access_point), activity_coverages)
        )
        public_access_points.append(
            {
                "id": access_point_id,
                **access_point,
                "nps_amenities": nps_amenities,
                "nps_recreation_matches": [
                    {
                        "name": locations[index]["name"],
                        "milepost": locations[index]["milepost"],
                        "milepost_distance": float(
                            round(
                                _milepost_distance(
                                    access_point, locations[index]["milepost"]
                                ),
                                2,
                            )
                        ),
                    }
                    for index in match_indexes
                ],
                "nearby_features": nearby_features,
                "coverage_status": status,
                "coverage_by_mode": coverage_by_mode,
                "covering_activity_count": activity_count,
                "covering_activity_count_by_mode": count_by_mode,
                "first_covering_activity": first_activity,
                "latest_covering_activity": latest_activity,
                "remaining_segments": _remaining_segments(
                    access_point, status, remaining
                ),
            }
        )

    public_locks = []
    for lock in locks:
        coverage_point = {"milepost": lock["milepost"], "milepost_end": None}
        status = _coverage_status(coverage_point, covered_bins)
        coverage_by_mode = {
            mode: _coverage_status(coverage_point, mode_coverage_bins[mode])
            for mode in ACTIVITY_MODES
        }
        activity_count, count_by_mode, first_activity, latest_activity = (
            _covering_history(_access_point_bins(coverage_point), activity_coverages)
        )
        public_locks.append(
            {
                "id": lock_ids[lock["lock_number"]],
                "lock_number": lock["lock_number"],
                "name": lock["name"],
                "common_name": lock["common_name"],
                "milepost": lock["milepost"],
                "coverage_status": status,
                "coverage_by_mode": coverage_by_mode,
                "covering_activity_count": activity_count,
                "covering_activity_count_by_mode": count_by_mode,
                "remaining_segments": _remaining_segments(
                    coverage_point, status, remaining
                ),
                "first_covering_activity": first_activity,
                "latest_covering_activity": latest_activity,
            }
        )
    return (
        {
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "sources": {
                "access_points": access_data["source"],
                "recreation_guide": recreation_data["source"],
                "locks": {
                    "name": "C&O Canal lift-lock reference",
                    "authority_url": "https://www.nps.gov/choh/learn/historyculture/lift-locks.htm",
                },
            },
            "derived": DERIVED_METADATA,
            "access_points": public_access_points,
        },
        {
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "sources": [
                {"name": source["name"], "url": source["url"]}
                for source in lock_data["sources"]
            ],
            "derived": DERIVED_METADATA,
            "locks": public_locks,
        },
    )


__all__ = [
    "AMENITY_FIELDS",
    "build_public_reference_artifacts",
    "load_access_points",
    "load_locks",
    "load_recreation_guide",
]
