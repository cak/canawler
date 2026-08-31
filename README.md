# Canawler

Canawler tracks progress running, biking, and hiking the official 184.5-mile Chesapeake & Ohio Canal towpath. Its primary metric is **unique C&O towpath mileage covered**: revisiting a completed section adds travel distance, but it does not increase overall completion.

## How it works

```text
Strava bulk export
        +
NPS-derived canonical C&O towpath geometry
        ↓
Python ingestion and geospatial matching
        ↓
unique C&O coverage calculation
        ↓
data/processed/
        ↓
privacy-safe publication data in data/public/
        ↓
Quarto presentation layer
```

`src/canawler/` is the authoritative ingestion, matching, coverage, validation, and publication implementation. Quarto may filter, group, sort, highlight, or animate the published results, but it does not parse GPS files or determine coverage.

The generated CSV and JSON are frontend-neutral. A future presentation layer, such as Vue, can consume `data/public/` without moving coverage calculations out of Python.

## Data sources

### C&O reference geometry

The canonical spatial reference is `data/reference/co-towpath/towpath.geojson`. It is derived from the National Park Service Public Trails GIS dataset for Chesapeake & Ohio Canal National Historical Park and is oriented from **Georgetown to Cumberland**.

The geometry supplies the route shape used for spatial matching. The official completion denominator remains 184.5 miles: the measured GIS length may differ slightly and does not redefine the official canal length. See [`data/reference/co-towpath/README.md`](data/reference/co-towpath/README.md) for source, selection, normalization, and provenance details.

### Strava activity data

Activity history comes from a full Strava bulk export placed under `data/raw/strava/`. Canawler intentionally does not use the Strava API. The export is treated as a complete, reproducible snapshot, and each build recalculates history and coverage from that snapshot.

Request a new full export after substantial new C&O activity. Otherwise, consider refreshing roughly every three months; if there has been no meaningful new C&O activity, there is no reason to refresh merely because three months have passed.

Canawler reads the export's `activities.csv` and its referenced gzip-compressed FIT, GPX, or TCX tracks. Only Strava types exactly equal to `Run`, `Ride`, or `Hike` are candidates; they become `run`, `bike`, and `hike` in Canawler output.

## Project data

| Path | Contents | Git policy |
| --- | --- | --- |
| `data/raw/` | Private source data, including complete Strava exports and GPS tracks. | Ignored except for `.gitkeep`; never commit source contents. |
| `data/reference/` | Stable public reference data required by the analytical engine. | Not ignored; commit the canonical reference and its provenance. |
| `data/processed/` | Detailed, reproducible analytical outputs for local inspection and audit. | Ignored except for `.gitkeep`. |
| `data/public/` | Deliberately sanitized, frontend-ready CSV and JSON. | Not ignored; intended to be committed and deployed. |
| `site/` | Current Quarto presentation code. | Not ignored; generated `_site/` and `.quarto/` are ignored. |

The data build produces these internal files:

- `data/processed/activities.csv`: one flat row per activity with qualifying C&O coverage.
- `data/processed/activities.json`: the same activity history plus richer metadata, matching diagnostics, and disconnected coverage segments.
- `data/processed/coverage.json`: authoritative overall, per-activity-type, completed, and remaining coverage summaries with methodology metadata.

It then publishes allowlisted fields to:

- `data/public/activities.csv`
- `data/public/activities.json`
- `data/public/coverage.json`

Public activity JSON retains C&O segment intervals; it does not retain private notes, source filenames, fitness diagnostics, exact timestamps, or tracks.

## Building the data

The normal end-to-end data build validates the committed reference, ingests the private export, calculates and structurally validates coverage, writes `data/processed/`, and sanitizes `data/public/`:

```console
uv run canawler build
```

When exactly one directory under `data/raw/strava/` contains `activities.csv`, the build finds it automatically. If there are zero or multiple exports, the command fails clearly; an explicit path remains available for that exceptional case:

```console
uv run canawler build --export data/raw/strava/export_309741
```

Audit uses the same complete rebuild and reports suspicious matches without changing coverage:

```console
uv run canawler audit
```

Reference inspection and rebuilding are explicit maintenance operations that contact the NPS service:

```console
uv run canawler reference inspect
uv run canawler reference build
```

Ordinary activity builds use the committed canonical GeoJSON and do not contact NPS. The Python data build and Quarto render are separate operations:

```console
quarto render site
```

## Coverage methodology

Canawler compares ordered GPS observations with the canonical towpath after projecting both into the meter-based **EPSG:5070** coordinate reference system. The current default proximity tolerance is **30 meters**.

Proximity alone does not establish continuous coverage. Consecutive observations must also satisfy limits on elapsed time, source-track distance, distance along the towpath, and movement alignment. Leaving the towpath and later rejoining it therefore does not fill the intervening section. Short or implausible matched sequences do not produce coverage. Canawler favors avoiding false completion over maximizing matched mileage.

Qualifying movement is represented on an official 0–184.5-mile linear reference using **0.01-mile bins**. A bin covered multiple times, by one activity or many, counts once toward overall completion. These bins are a stable coverage representation, not a claim that consumer GPS measurements are accurate to 0.01 miles.

Activities are ordered by their normalized UTC activity timestamp and then by Activity ID as a deterministic tie-breaker. New and cumulative coverage are computed in that order. The build validates chronology, coverage unions, activity-type unions, milepost bounds, and the completed/remaining partition before publishing.

## Processed activity data

`data/processed/activities.csv` contains one row for each `run`, `bike`, or `hike` whose accepted movement produces at least one C&O coverage bin. It is a detailed internal analytical table, not a public export of the Strava account.

### Mileage definitions

**Activity distance — `strava_distance_miles`**

The total distance of the entire activity as reported in the Strava export, converted from meters to miles. It may include travel away from the C&O.

**C&O travel distance — `co_travel_miles`**

Canawler's estimate of distance traveled along qualifying C&O portions. Repeated travel counts repeatedly: five C&O miles out and five miles back can contribute approximately ten C&O travel miles. This calculation uses along-towpath movement and may differ slightly from Strava's distance processing. The implementation does not cap it at Strava distance.

**Unique C&O miles — `co_unique_miles`**

The number of distinct 0.01-mile C&O bins touched by this activity. Repeated travel over a bin counts once. The same five-mile out-and-back could therefore have about ten C&O travel miles but five unique C&O miles.

**New C&O unique miles — `new_co_unique_miles`**

The activity's unique bins that were not covered by any chronologically earlier qualifying Run, Ride, or Hike. This is the mileage by which the activity advances overall Canawler completion.

**Cumulative unique miles — `cumulative_unique_miles`**

The union of all C&O coverage through this activity in chronological order. It never decreases.

**Cumulative completion — `cumulative_percent_complete`**

Cumulative unique mileage divided by the official 184.5-mile towpath length and multiplied by 100. Values are percentages on a **0–100 scale**, not fractions on a 0–1 scale.

Unique mileage is bin-based while travel mileage is a continuous along-route estimate. A short qualifying leg can touch a full 0.01-mile bin while contributing slightly less than 0.01 travel miles, so unique mileage is not required to be less than travel mileage.

### `activities.csv` columns

CSV values are textual cells, but numeric fields originate as Python numbers and are numbers in `activities.json`. Missing optional metadata is written as an empty CSV cell and `null` in JSON.

| Column | Type / unit | Description |
| --- | --- | --- |
| `activity_id` | Integer identifier | Strava's unique activity identifier. Written as decimal digits in CSV and as a JSON number. |
| `date` | Date, `YYYY-MM-DD` | UTC calendar date derived from the normalized activity timestamp. |
| `start_time` | UTC time, `HH:MM:SSZ` | UTC time-of-day derived from the activity timestamp, serialized to whole seconds. This is not presented as local time. |
| `datetime` | ISO 8601 UTC timestamp | Combined timestamp used for chronological ordering, serialized with a trailing `Z`; current generated values use `YYYY-MM-DDTHH:MM:SSZ`. Activity ID breaks timestamp ties. |
| `activity_name` | String | Activity title from the Strava export. |
| `activity_type` | Enum | Normalized Canawler type: `run`, `bike`, or `hike`. |
| `strava_distance_miles` | Miles, 3 decimals | Total Strava activity distance, including non-C&O portions. The parser normalizes the export's distance field to meters, then converts it to miles. May be blank. |
| `co_travel_miles` | Miles, 3 decimals | Estimated along-towpath travel on qualifying C&O legs. Repeated traversal counts repeatedly and the value is not capped at Strava distance. |
| `co_unique_miles` | Miles, 2 decimals | Distinct 0.01-mile C&O bins covered by this activity; repeated traversal of a bin counts once. |
| `new_co_unique_miles` | Miles, 2 decimals | This activity's unique bins not covered by any earlier qualifying activity. |
| `cumulative_unique_miles` | Miles, 2 decimals | Union of all covered bins through this activity chronologically. |
| `cumulative_percent_complete` | Percent, 0–100, 3 decimals | `cumulative_unique_miles / 184.5 × 100`, calculated from the underlying bin count before display rounding. |
| `min_milepost` | C&O milepost, 2 decimals | Start of the lowest covered bin for the activity. It is a range summary and does not imply continuous coverage through `max_milepost`. |
| `max_milepost` | C&O milepost, 2 decimals | End of the highest covered bin. Disconnected uncovered sections may exist below it; inspect `activities.json` for actual segments. |
| `moving_time_minutes` | Minutes, 2 decimals | Strava moving time converted from seconds to minutes. It generally excludes time Strava classified as stopped. May be blank. |
| `elapsed_time_minutes` | Minutes, 2 decimals | Strava total elapsed time converted from seconds to minutes, including stopped time. May be blank. |
| `elevation_gain_feet` | Feet, 1 decimal | Strava elevation-gain metadata converted from meters to feet; Canawler does not recompute it from the track. May be blank. |
| `average_heart_rate` | Beats per minute, 1 decimal | Average heart rate from the Strava export. May be blank. |
| `max_heart_rate` | Beats per minute, 1 decimal | Maximum heart rate from the Strava export. May be blank. |
| `calories` | Strava-reported calories, 1 decimal | Calorie value from the Strava export. May be blank and is not independently measured or validated by Canawler. |
| `matched_point_count` | Integer observations | Number of usable GPS observations whose nearest towpath geometry is within the configured proximity tolerance. This point-level diagnostic is calculated before continuity and minimum-run checks, so it is not a count of points that necessarily produced coverage bins. |
| `total_gps_point_count` | Integer observations | Total finite, in-range, geolocated observations parsed from the activity track. An observation can count even if its timestamp is absent; points without usable latitude/longitude do not count. |
| `match_percentage` | Percent, 0–100, 2 decimals | `matched_point_count / total_gps_point_count × 100`. A low value can be legitimate when one activity intentionally includes both C&O and non-C&O portions. |
| `strava_url` | URL | Convenience URL formed from the Activity ID. Whether it is accessible still depends on the activity's Strava privacy settings. |

Important interpretation caveats:

1. One row represents one activity with at least one qualifying C&O coverage bin.
2. `min_milepost` and `max_milepost` summarize extent; they do not prove that every section between them was covered.
3. C&O travel, unique C&O, and new C&O unique mileage answer different questions and are not interchangeable.
4. Canawler and Strava can report slightly different distances because they use different GPS processing and measurement methods.
5. Missing sensor or fitness metadata remains blank/`null`; Canawler does not estimate it.
6. Neither processed activity file contains raw GPS coordinates or complete tracks.

The Strava `Activity Date` value is parsed with UTC normalization: timezone-aware input is converted to UTC, while timezone-naive input is interpreted as UTC. The `date`, `start_time`, and `datetime` fields reflect that normalized timestamp.

### `activities.json`

JSON preserves structures that do not fit naturally in the flat CSV. In addition to the CSV fields, each processed activity record contains:

- `segments`: ordered, possibly disconnected C&O intervals, each with `start_milepost`, `end_milepost`, and `miles`.
- `original_activity_type`: the original Strava type before normalization.
- `description` and `filename`: private source metadata retained for local audit.
- `elevation_loss_feet`, `average_speed_mps`, and `max_speed_mps`: additional Strava metadata.
- `matching_diagnostics`: median and 90th-percentile point-to-reference distance for proximity-accepted points, plus a median alignment ratio for plausible movement legs when calculable.

The JSON does not contain parsed GPS observations or coordinate arrays. It remains private because it includes source metadata, exact timestamps, and fitness data.

## Coverage data

`data/processed/coverage.json` is the authoritative project-level summary. Its top-level data includes:

- `canal_miles` and `bin_miles`.
- `combined_unique_miles`, `combined_percent_complete`, and `remaining_miles`.
- `run_unique_miles`, `bike_unique_miles`, and `hike_unique_miles`, each calculated as a union for that activity type. Their sum can exceed combined coverage where types overlap.
- `activity_count`, `first_activity_date`, and `latest_activity_date` for qualifying covered activities.
- `completed_segments`, `remaining_segments`, and `longest_continuous_completed_section`; interval objects contain `start_milepost`, `end_milepost`, and `miles`.
- `methodology`, including matching tolerance, continuity settings, audit thresholds, projected CRS, and a SHA-256 hash of the canonical reference.

Completed and remaining segments partition the full 0–184.5-mile reference. Public `coverage.json` retains the frontend-relevant totals, intervals, dates, and methodology.

## Privacy

Full Strava exports contain sensitive historical GPS, location, account, and fitness information. They must remain under gitignored `data/raw/`; detailed generated files under `data/processed/` are also gitignored.

Commit-eligible `data/public/` is an explicit allowlist intended to describe C&O progress, not reproduce the Strava account. It excludes raw coordinates and tracks, private notes, source filenames and local paths, exact start times, heart rate, calories, authentication data, gear identifiers, matching diagnostics, and exact non-C&O locations. Public activity records retain only the fields needed to present C&O progress, including dates, mileage, milepost summaries, C&O intervals, and Strava links.

## Development

Install the locked Python environment, run Ruff, and render the presentation layer separately:

```console
uv sync
uv run ruff check .
quarto render site
```

No Markdown-specific linter is currently configured.
