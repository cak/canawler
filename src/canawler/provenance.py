"""Build the deterministic public registry of external data sources."""

from __future__ import annotations

from typing import Any

from canawler.reference import FEATURESERVER_URL, ReferenceDataError
from canawler.reference_artifacts import (
    ACCESS_POINTS_SOURCE_ID,
    CANAL_TRUST_LOCKS_SOURCE_ID,
    NPS_LOCKS_SOURCE_ID,
    RECREATION_GUIDE_SOURCE_ID,
    load_access_points,
    load_locks,
    load_recreation_guide,
)

NPS_TOWPATH_SOURCE_ID = "nps_towpath_reference"
STRAVA_SOURCE_ID = "strava_activity_data"
SOURCE_SCHEMA_VERSION = 1


def _relationships(*items: tuple[str, str]) -> list[dict[str, str]]:
    return [{"source_id": source_id, "role": role} for source_id, role in sorted(items)]


def _all_source_relationships(source_ids: list[str], role: str) -> list[dict[str, str]]:
    return _relationships(*((source_id, role) for source_id in source_ids))


def _lock_source(sources: list[dict[str, Any]], role: str) -> dict[str, Any]:
    matches = [source for source in sources if source.get("role") == role]
    if len(matches) != 1:
        raise ReferenceDataError(f"expected one lock source with role {role!r}")
    return matches[0]


def build_public_source_registry() -> dict[str, Any]:
    """Consolidate existing reference provenance and public artifact derivations."""
    access_source = load_access_points()["source"]
    recreation_source = load_recreation_guide()["source"]
    lock_sources = load_locks()["sources"]
    nps_locks = _lock_source(lock_sources, "Lift-lock count and numbering authority")
    canal_trust = _lock_source(
        lock_sources, "Primary practical lock mileposts and common names"
    )

    sources = sorted(
        [
            {
                "id": ACCESS_POINTS_SOURCE_ID,
                "name": access_source["name"],
                "organization": "C&O Canal Association",
                "url": access_source["url"],
                "description": (
                    "Canonical automobile access-point names, source milepoints and "
                    "ranges, latitude, and longitude used by Canawler."
                ),
                "license": None,
            },
            {
                "id": CANAL_TRUST_LOCKS_SOURCE_ID,
                "name": canal_trust["name"],
                "organization": "C&O Canal Trust",
                "url": canal_trust["url"],
                "description": (
                    "Practical lift-lock mileposts and common-name information used "
                    "by Canawler's curated lock reference dataset."
                ),
                "license": None,
            },
            {
                "id": NPS_LOCKS_SOURCE_ID,
                "name": "National Park Service Lift Locks",
                "organization": "National Park Service",
                "url": nps_locks["url"],
                "description": (
                    "Authority used by Canawler for the C&O Canal lift-lock count "
                    "and numbering, including the 74-lock total and fractional "
                    "numbering."
                ),
                "license": None,
            },
            {
                "id": RECREATION_GUIDE_SOURCE_ID,
                "name": recreation_source["name"],
                "organization": "National Park Service",
                "url": recreation_source["url"],
                "description": (
                    "Recreation locations and listed visitor amenities used to "
                    "enrich canonical access points and identify selected nearby "
                    "recreation features."
                ),
                "license": None,
            },
            {
                "id": NPS_TOWPATH_SOURCE_ID,
                "name": "National Park Service Public Trails dataset",
                "organization": "National Park Service",
                "url": FEATURESERVER_URL,
                "description": (
                    "Underlying trail data for the canonical towpath geometry used "
                    "by Canawler's GPS-to-canal matching and milepost coverage "
                    "calculations."
                ),
                "license": None,
            },
            {
                "id": STRAVA_SOURCE_ID,
                "name": "Strava activity data",
                "organization": "Strava",
                "url": "https://www.strava.com/",
                "description": (
                    "User activity data originating from Strava and used as input "
                    "to Canawler's activity and coverage calculations."
                ),
                "license": None,
            },
        ],
        key=lambda source: source["id"],
    )
    source_ids = [source["id"] for source in sources]
    if len(source_ids) != len(set(source_ids)):
        raise ReferenceDataError("public source IDs must be unique")

    activity_sources = _relationships(
        (
            NPS_TOWPATH_SOURCE_ID,
            "Towpath geometry input for calculated C&O matching and coverage fields",
        ),
        (STRAVA_SOURCE_ID, "User activity-data input"),
    )
    feature_coverage_sources = (
        (
            NPS_TOWPATH_SOURCE_ID,
            "Input to Canawler-derived towpath coverage fields",
        ),
        (STRAVA_SOURCE_ID, "Input to Canawler-derived activity history fields"),
    )
    access_sources = (
        (ACCESS_POINTS_SOURCE_ID, "Canonical automobile access-point facts"),
        (
            RECREATION_GUIDE_SOURCE_ID,
            "Amenity enrichment and selected nearby recreation features",
        ),
        (NPS_LOCKS_SOURCE_ID, "Lift-lock count and numbering authority"),
        (
            CANAL_TRUST_LOCKS_SOURCE_ID,
            "Practical lock mileposts and common names for nearby locks",
        ),
    )
    all_registry_sources = _all_source_relationships(
        source_ids, "External source represented in the public provenance registry"
    )

    artifacts = {
        "access-point-nps-matches.csv": {
            "sources": _relationships(
                (
                    ACCESS_POINTS_SOURCE_ID,
                    "Canonical automobile access-point facts",
                ),
                (RECREATION_GUIDE_SOURCE_ID, "NPS recreation-guide rows"),
            ),
            "derived_by": [
                "Canawler matching of NPS recreation-guide rows to canonical automobile access points",
                "Canawler downstream normalization of access-points.json",
            ],
        },
        "access-points.json": {
            "sources": _relationships(*access_sources, *feature_coverage_sources),
            "derived_by": [
                "Canawler stable public identifiers",
                "Canawler access-point and NPS recreation-guide matching",
                "Canawler NPS amenity aggregation across matched rows",
                "Canawler nearby-feature milepost relationships",
                "Canawler combined and per-mode towpath coverage calculations",
                "Canawler per-activity coverage matching and activity counts",
                "Canawler remaining-segment relationships",
            ],
        },
        "activities.csv": {
            "sources": activity_sources,
            "derived_by": [
                "Canawler GPS-to-towpath matching",
                "Canawler calculated C&O travel and unique miles, matched segments, milepost ranges, and cumulative completion",
            ],
        },
        "activities.json": {
            "sources": activity_sources,
            "derived_by": [
                "Canawler GPS-to-towpath matching",
                "Canawler calculated C&O travel and unique miles, matched segments, milepost ranges, and cumulative completion",
            ],
        },
        "artifact-sources.csv": {
            "sources": _all_source_relationships(
                source_ids, "External source represented in artifact relationships"
            ),
            "derived_by": [
                "Canawler normalization of sources.json artifact and source relationships"
            ],
        },
        "coverage-segments.csv": {
            "sources": activity_sources,
            "derived_by": [
                "Canawler coverage calculation",
                "Canawler downstream normalization of coverage.json published segments",
            ],
        },
        "coverage.json": {
            "sources": activity_sources,
            "derived_by": [
                "Canawler GPS-to-towpath matching",
                "Canawler 0.01-mile coverage bins",
                "Canawler combined, run, bike, and hike coverage",
                "Canawler completed and remaining segments and coverage totals",
            ],
        },
        "feature-nearby-features.csv": {
            "sources": _relationships(*access_sources),
            "derived_by": [
                "Canawler milepost-based nearby-feature relationships within the published one-mile threshold",
                "Canawler downstream normalization of access-points.json",
            ],
        },
        "features.csv": {
            "sources": _relationships(*access_sources, *feature_coverage_sources),
            "derived_by": [
                "Canawler downstream normalization of its public lock and access-point JSON artifacts for analytical use"
            ],
        },
        "locks.json": {
            "sources": _relationships(
                (
                    CANAL_TRUST_LOCKS_SOURCE_ID,
                    "Practical lock mileposts and common names",
                ),
                (NPS_LOCKS_SOURCE_ID, "Lift-lock count and numbering authority"),
                *feature_coverage_sources,
            ),
            "derived_by": [
                "Canawler stable public identifiers",
                "Canawler combined and per-mode towpath coverage status",
                "Canawler covering activity counts and first and latest covering activities",
                "Canawler remaining-segment relationships",
            ],
        },
        "sources.csv": {
            "sources": all_registry_sources,
            "derived_by": [
                "Canawler normalization of sources.json external source records"
            ],
        },
        "sources.json": {
            "sources": all_registry_sources,
            "derived_by": [
                "Canawler consolidation of existing source provenance and public artifact derivations"
            ],
        },
    }

    return {
        "schema_version": SOURCE_SCHEMA_VERSION,
        "sources": sources,
        "artifacts": dict(sorted(artifacts.items())),
    }


__all__ = ["build_public_source_registry"]
