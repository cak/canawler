"""Build Canawler's canonical C&O Canal towpath reference geometry.

The NPS layer contains many CHOH side trails. Selection is deliberately narrow:
records must carry the established towpath mile-segment naming convention (including
known source spelling variants), or match the one explicitly named bridge that NPS
labels as Towpath. Any other record that looks like a towpath candidate is an error,
so a source-schema change cannot silently expand the route.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pyproj import Geod
from shapely.geometry import LineString, mapping, shape

FEATURESERVER_URL = (
    "https://mapservices.nps.gov/arcgis/rest/services/NationalDatasets/"
    "NPS_Public_Trails_Geographic/FeatureServer/0"
)
QUERY_URL = f"{FEATURESERVER_URL}/query"
UNIT_CODE = "CHOH"
DEFAULT_OUTPUT = Path("data/reference/co-towpath/towpath.geojson")
DEFAULT_METADATA = Path("data/reference/co-towpath/README.md")
PUBLIC_DIR = Path("data/public")
PUBLIC_JSON_DIR = PUBLIC_DIR / "json"
PUBLIC_CSV_DIR = PUBLIC_DIR / "csv"


def public_format_directories(public_directory: Path = PUBLIC_DIR) -> tuple[Path, Path]:
    """Return the canonical JSON and derived CSV directories for a public root."""
    public_directory = Path(public_directory)
    return (
        public_directory / PUBLIC_JSON_DIR.name,
        public_directory / PUBLIC_CSV_DIR.name,
    )


INSPECTION_FIELDS = (
    "OBJECTID",
    "UNITCODE",
    "TRLNAME",
    "TRLALTNAME",
    "MAPLABEL",
    "TRLSTATUS",
    "TRLSURFACE",
    "TRLUSE",
    "TRLTYPE",
    "TRLCLASS",
    "TRLFEATTYPE",
    "PUBLICDISPLAY",
    "DATAACCESS",
    "ISEXTANT",
    "OPENTOPUBLIC",
    "GEOMETRYID",
)
REQUIRED_FIELDS = frozenset(INSPECTION_FIELDS)

GEORGETOWN = (-77.0567, 38.9041)
CUMBERLAND = (-78.7640, 39.6481)
ENDPOINT_TOLERANCE_METERS = 3_000.0
EXACT_ENDPOINT_TOLERANCE_METERS = 1.0
MAX_SOURCE_GAP_METERS = 50.0
METERS_PER_MILE = 1609.344

_MILE_SEGMENT = re.compile(
    r"Towpath\s*,?\s*Mile(?:s?post)?\s+\d+\s*-\s*\d+"
    r"(?:\s+\(\d+(?:\.\d+)?\))?",
    re.IGNORECASE,
)
_EXPLICIT_NAMES = {
    "Towpath, Milepost 184.184.5 (184.00)",
    "Widewater Waste Weir Bridge 3100-059S",
}
_EXPECTED_ATTRIBUTES = {
    "TRLALTNAME": "Potomac Heritage National Scenic Trail",
    "TRLSTATUS": "Existing",
    "TRLTYPE": "Standard Terra Trail",
    "TRLFEATTYPE": "Park Trail",
    "PUBLICDISPLAY": "Public Map Display",
    "DATAACCESS": "Unrestricted",
    "ISEXTANT": "True",
}
_EXPECTED_USES = {"hiker / pedestrian", "bicycle"}
_GEOD = Geod(ellps="WGS84")

Feature = dict[str, Any]
FeatureCollection = dict[str, Any]


class ReferenceDataError(RuntimeError):
    """The source data cannot be converted safely into the canonical reference."""


@dataclass(frozen=True)
class BuildReport:
    source_features: int
    selected_features: int
    geometry_type: str
    eastern_endpoint: tuple[float, float]
    western_endpoint: tuple[float, float]
    length_miles: float
    is_valid: bool
    output_path: Path

    def format(self) -> str:
        return "\n".join(
            (
                f"Source CHOH features: {self.source_features}",
                f"Selected towpath features: {self.selected_features}",
                f"Geometry type: {self.geometry_type}",
                (
                    f"Eastern endpoint (Georgetown): "
                    f"{self.eastern_endpoint[0]:.6f}, "
                    f"{self.eastern_endpoint[1]:.6f}"
                ),
                (
                    f"Western endpoint (Cumberland): "
                    f"{self.western_endpoint[0]:.6f}, "
                    f"{self.western_endpoint[1]:.6f}"
                ),
                f"Geometric length: {self.length_miles:.3f} miles",
                f"Geometry valid: {self.is_valid}",
                f"Output: {self.output_path}",
            )
        )


@dataclass(frozen=True)
class _SourceEdge:
    feature_id: int | str | None
    coordinates: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class _GraphEdge:
    start: int
    end: int
    coordinates: tuple[tuple[float, float], ...]
    source_edge: int | None


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").split())


def _distance(left: tuple[float, float], right: tuple[float, float]) -> float:
    return _GEOD.inv(left[0], left[1], right[0], right[1])[2]


def _query_url() -> str:
    parameters = urlencode(
        {
            "where": "UNITCODE = 'CHOH'",
            "outFields": ",".join(INSPECTION_FIELDS),
            "returnGeometry": "true",
            "outSR": "4326",
            "orderByFields": "OBJECTID",
            "f": "geojson",
        }
    )
    return f"{QUERY_URL}?{parameters}"


def fetch_choh_geojson(
    opener: Callable[..., BinaryIO] = urlopen,
) -> FeatureCollection:
    """Fetch CHOH public trails as WGS84 GeoJSON directly from ArcGIS."""
    request = Request(
        _query_url(), headers={"User-Agent": "Canawler reference-data builder/0.1"}
    )
    try:
        with opener(request, timeout=60) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise ReferenceDataError(
            f"NPS FeatureServer request failed: {error}"
        ) from error

    if not isinstance(payload, dict):
        raise ReferenceDataError("NPS response is not a GeoJSON object")
    if "error" in payload:
        raise ReferenceDataError(
            f"NPS FeatureServer returned an error: {payload['error']}"
        )
    if payload.get("type") != "FeatureCollection" or not isinstance(
        payload.get("features"), list
    ):
        raise ReferenceDataError("NPS response is not a GeoJSON FeatureCollection")
    if payload.get("exceededTransferLimit"):
        raise ReferenceDataError("NPS response was truncated by the FeatureServer")

    choh = filter_choh_features(payload["features"])
    _validate_schema(choh)
    return {"type": "FeatureCollection", "features": choh}


def filter_choh_features(features: Iterable[Feature]) -> list[Feature]:
    """Defensively retain only features whose returned UNITCODE is CHOH."""
    return [
        feature
        for feature in features
        if _normalized(feature.get("properties", {}).get("UNITCODE")).upper()
        == UNIT_CODE
    ]


def _validate_schema(features: Sequence[Feature]) -> None:
    if not features:
        raise ReferenceDataError("NPS returned no CHOH trail features")
    actual_fields: set[str] = set()
    for feature in features:
        properties = feature.get("properties")
        if isinstance(properties, dict):
            actual_fields.update(properties)
    missing = sorted(REQUIRED_FIELDS - actual_fields)
    if missing:
        raise ReferenceDataError(
            "NPS trail schema is missing required returned fields: "
            + ", ".join(missing)
        )


def inspect_reference(source: FeatureCollection, output: Any = None) -> None:
    """Print distinct CHOH names and labels before towpath selection."""
    import sys

    output = output or sys.stdout
    features = filter_choh_features(source.get("features", []))
    _validate_schema(features)
    fields = sorted(
        {field for feature in features for field in feature.get("properties", {})}
    )
    print(f"CHOH features: {len(features)}", file=output)
    print(f"Returned fields: {', '.join(fields)}", file=output)
    print(
        "COUNT\tTRLNAME\tTRLALTNAME\tMAPLABEL\tTRLSTATUS\tTRLSURFACE\tTRLUSE",
        file=output,
    )
    rows = Counter(
        tuple(
            _normalized(feature["properties"].get(field))
            for field in (
                "TRLNAME",
                "TRLALTNAME",
                "MAPLABEL",
                "TRLSTATUS",
                "TRLSURFACE",
                "TRLUSE",
            )
        )
        for feature in features
    )
    for row, count in sorted(rows.items(), key=lambda item: item[0]):
        print(f"{count}\t" + "\t".join(row), file=output)


def _is_candidate(properties: dict[str, Any]) -> bool:
    name = _normalized(properties.get("TRLNAME")).casefold()
    label = _normalized(properties.get("MAPLABEL")).casefold()
    return name.startswith("towpath") or label == "towpath"


def _has_expected_attributes(properties: dict[str, Any]) -> bool:
    attributes_match = all(
        _normalized(properties.get(field)).casefold() == expected.casefold()
        for field, expected in _EXPECTED_ATTRIBUTES.items()
    )
    uses = {
        _normalized(item).casefold()
        for item in _normalized(properties.get("TRLUSE")).split("|")
    }
    return attributes_match and _EXPECTED_USES <= uses


def _has_towpath_name(properties: dict[str, Any]) -> bool:
    name = _normalized(properties.get("TRLNAME"))
    return bool(_MILE_SEGMENT.fullmatch(name)) or name in _EXPLICIT_NAMES


def _feature_summary(feature: Feature) -> str:
    properties = feature.get("properties", {})
    return ", ".join(
        f"{field}={properties.get(field)!r}"
        for field in (
            "OBJECTID",
            "TRLNAME",
            "TRLALTNAME",
            "MAPLABEL",
            "TRLSTATUS",
            "TRLSURFACE",
            "TRLUSE",
            "ISEXTANT",
        )
    )


def select_towpath_features(features: Iterable[Feature]) -> list[Feature]:
    """Select explicit towpath records and reject unrecognized candidates."""
    selected: list[Feature] = []
    ambiguous: list[Feature] = []
    for feature in filter_choh_features(features):
        properties = feature.get("properties", {})
        if not _is_candidate(properties):
            continue
        if _has_towpath_name(properties) and _has_expected_attributes(properties):
            selected.append(feature)
        else:
            ambiguous.append(feature)

    if ambiguous:
        details = "\n  - ".join(_feature_summary(item) for item in ambiguous)
        raise ReferenceDataError(
            "unrecognized or attribute-mismatched towpath candidate(s); add an "
            f"explicit inclusion rule only after review:\n  - {details}"
        )
    if not selected:
        raise ReferenceDataError("no unambiguous C&O towpath features were found")
    return sorted(
        selected,
        key=lambda feature: (
            int(feature.get("properties", {}).get("OBJECTID") or 0),
            _normalized(feature.get("properties", {}).get("GEOMETRYID")),
        ),
    )


def _source_edges(features: Sequence[Feature]) -> list[_SourceEdge]:
    edges: list[_SourceEdge] = []
    seen: set[tuple[tuple[float, float], ...]] = set()
    for feature in features:
        raw_geometry = feature.get("geometry")
        if not raw_geometry:
            raise ReferenceDataError(
                f"selected feature has no geometry: {_feature_summary(feature)}"
            )
        geometry = shape(raw_geometry)
        if geometry.is_empty or not geometry.is_valid:
            raise ReferenceDataError(
                f"selected feature has invalid geometry: {_feature_summary(feature)}"
            )
        if geometry.geom_type == "LineString":
            lines = [geometry]
        elif geometry.geom_type == "MultiLineString":
            lines = list(geometry.geoms)
        else:
            raise ReferenceDataError(
                f"selected feature has unsupported {geometry.geom_type} geometry"
            )
        for line in lines:
            coordinates_list: list[tuple[float, float]] = []
            for coordinate in line.coords:
                point = (float(coordinate[0]), float(coordinate[1]))
                if not coordinates_list or point != coordinates_list[-1]:
                    coordinates_list.append(point)
            coordinates = tuple(coordinates_list)
            if len(coordinates) < 2:
                raise ReferenceDataError(
                    "selected towpath geometry has fewer than two points"
                )
            for longitude, latitude in coordinates:
                if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
                    raise ReferenceDataError(
                        "selected coordinates are not valid WGS84 longitude/latitude"
                    )
            canonical = min(coordinates, tuple(reversed(coordinates)))
            if canonical in seen:
                continue
            seen.add(canonical)
            edges.append(
                _SourceEdge(feature["properties"].get("OBJECTID"), coordinates)
            )
    return edges


class _DisjointSet:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: int, right: int) -> bool:
        left = self.find(left)
        right = self.find(right)
        if left == right:
            return False
        if right < left:
            left, right = right, left
        self.parent[right] = left
        return True


def _cluster_endpoints(
    edges: Sequence[_SourceEdge],
) -> tuple[list[tuple[float, float]], list[tuple[int, int]]]:
    endpoints = [
        coordinate
        for edge in edges
        for coordinate in (edge.coordinates[0], edge.coordinates[-1])
    ]
    groups = _DisjointSet(len(endpoints))
    for right, right_coordinate in enumerate(endpoints):
        for left, left_coordinate in enumerate(endpoints[:right]):
            if (
                _distance(left_coordinate, right_coordinate)
                <= EXACT_ENDPOINT_TOLERANCE_METERS
            ):
                groups.union(left, right)

    roots = sorted({groups.find(index) for index in range(len(endpoints))})
    node_for_root = {root: node for node, root in enumerate(roots)}
    coordinates: list[tuple[float, float]] = []
    for root in roots:
        members = sorted(
            endpoints[index]
            for index in range(len(endpoints))
            if groups.find(index) == root
        )
        coordinates.append(members[0])
    edge_nodes = [
        (
            node_for_root[groups.find(index * 2)],
            node_for_root[groups.find(index * 2 + 1)],
        )
        for index in range(len(edges))
    ]
    return coordinates, edge_nodes


def _components(
    node_count: int, graph_edges: Sequence[_GraphEdge]
) -> tuple[list[int], list[tuple[set[int], int]]]:
    adjacency: dict[int, list[int]] = defaultdict(list)
    for edge_index, edge in enumerate(graph_edges):
        adjacency[edge.start].append(edge_index)
        adjacency[edge.end].append(edge_index)

    component_for_node = [-1] * node_count
    components: list[tuple[set[int], int]] = []
    for start in range(node_count):
        if component_for_node[start] >= 0:
            continue
        component_index = len(components)
        nodes = {start}
        edge_indexes: set[int] = set()
        queue = [start]
        component_for_node[start] = component_index
        while queue:
            node = queue.pop()
            for edge_index in adjacency[node]:
                edge_indexes.add(edge_index)
                edge = graph_edges[edge_index]
                neighbor = edge.end if edge.start == node else edge.start
                if component_for_node[neighbor] < 0:
                    component_for_node[neighbor] = component_index
                    nodes.add(neighbor)
                    queue.append(neighbor)
        components.append((nodes, len(edge_indexes)))
    return component_for_node, components


def _build_route_graph(
    source_edges: Sequence[_SourceEdge],
) -> tuple[list[tuple[float, float]], list[_GraphEdge]]:
    node_coordinates, edge_nodes = _cluster_endpoints(source_edges)
    graph_edges: list[_GraphEdge] = []
    for edge_index, (start, end) in enumerate(edge_nodes):
        if start == end:
            raise ReferenceDataError(
                f"towpath source edge {source_edges[edge_index].feature_id!r} "
                "collapses to a loop at endpoint tolerance"
            )
        graph_edges.append(
            _GraphEdge(start, end, source_edges[edge_index].coordinates, edge_index)
        )

    component_for_node, components = _components(len(node_coordinates), graph_edges)
    for nodes, edge_count in components:
        if edge_count != len(nodes) - 1:
            raise ReferenceDataError(
                "selected towpath geometry contains a cycle or alternate route; "
                "an explicit inclusion rule is required"
            )

    component_groups = _DisjointSet(len(components))
    candidates: list[tuple[float, int, int]] = []
    closest_gap = float("inf")
    for right, right_coordinate in enumerate(node_coordinates):
        for left, left_coordinate in enumerate(node_coordinates[:right]):
            if component_for_node[left] == component_for_node[right]:
                continue
            distance = _distance(left_coordinate, right_coordinate)
            closest_gap = min(closest_gap, distance)
            if distance <= MAX_SOURCE_GAP_METERS:
                candidates.append((distance, left, right))

    connectors = 0
    for _, left, right in sorted(candidates):
        left_component = component_for_node[left]
        right_component = component_for_node[right]
        if not component_groups.union(left_component, right_component):
            continue
        graph_edges.append(
            _GraphEdge(
                left,
                right,
                (node_coordinates[left], node_coordinates[right]),
                None,
            )
        )
        connectors += 1

    if connectors != len(components) - 1:
        gap_text = "unknown" if closest_gap == float("inf") else f"{closest_gap:.1f} m"
        raise ReferenceDataError(
            "selected towpath chains cannot be joined within the "
            f"{MAX_SOURCE_GAP_METERS:.0f} m source-gap limit "
            f"(closest unresolved gap: {gap_text})"
        )
    return node_coordinates, graph_edges


def _closest_node(
    node_coordinates: Sequence[tuple[float, float]], anchor: tuple[float, float]
) -> tuple[int, float]:
    node, coordinate = min(
        enumerate(node_coordinates),
        key=lambda item: (_distance(item[1], anchor), item[0]),
    )
    return node, _distance(coordinate, anchor)


def _route_edge_indexes(
    node_count: int,
    graph_edges: Sequence[_GraphEdge],
    start: int,
    end: int,
) -> list[tuple[int, int, int]]:
    adjacency: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for edge_index, edge in enumerate(graph_edges):
        adjacency[edge.start].append((edge.end, edge_index))
        adjacency[edge.end].append((edge.start, edge_index))
    if len(adjacency[start]) != 1 or len(adjacency[end]) != 1:
        raise ReferenceDataError(
            "geographic towpath endpoints are not terminal nodes after normalization"
        )

    previous: dict[int, tuple[int, int] | None] = {start: None}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        if node == end:
            break
        for neighbor, edge_index in sorted(adjacency[node]):
            if neighbor not in previous:
                previous[neighbor] = (node, edge_index)
                queue.append(neighbor)
    if end not in previous:
        raise ReferenceDataError("towpath endpoints are disconnected")

    route: list[tuple[int, int, int]] = []
    node = end
    while node != start:
        parent = previous[node]
        assert parent is not None
        prior, edge_index = parent
        route.append((prior, node, edge_index))
        node = prior
    route.reverse()
    if not route or node_count < 2:
        raise ReferenceDataError("towpath route has no usable edges")
    return route


def normalize_towpath_geometry(features: Sequence[Feature]) -> LineString:
    """Deduplicate, join bounded source gaps, and orient the unique route east-west."""
    source_edges = _source_edges(features)
    node_coordinates, graph_edges = _build_route_graph(source_edges)
    georgetown_node, georgetown_distance = _closest_node(node_coordinates, GEORGETOWN)
    cumberland_node, cumberland_distance = _closest_node(node_coordinates, CUMBERLAND)
    if georgetown_distance > ENDPOINT_TOLERANCE_METERS:
        raise ReferenceDataError(
            f"eastern endpoint is {georgetown_distance:.0f} m from Georgetown"
        )
    if cumberland_distance > ENDPOINT_TOLERANCE_METERS:
        raise ReferenceDataError(
            f"western endpoint is {cumberland_distance:.0f} m from Cumberland"
        )

    route = _route_edge_indexes(
        len(node_coordinates), graph_edges, georgetown_node, cumberland_node
    )
    coordinates: list[tuple[float, float]] = []
    for start, end, edge_index in route:
        edge = graph_edges[edge_index]
        edge_coordinates = edge.coordinates
        if edge.start != start or edge.end != end:
            edge_coordinates = tuple(reversed(edge_coordinates))
        if coordinates and coordinates[-1] != edge_coordinates[0]:
            coordinates.append(edge_coordinates[0])
        coordinates.extend(
            edge_coordinates if not coordinates else edge_coordinates[1:]
        )

    geometry = LineString(coordinates)
    if geometry.is_empty or not geometry.is_valid or not geometry.is_simple:
        raise ReferenceDataError(
            "normalized towpath is empty, invalid, or self-intersecting"
        )
    eastern = tuple(geometry.coords[0])
    western = tuple(geometry.coords[-1])
    if _distance(eastern, GEORGETOWN) > ENDPOINT_TOLERANCE_METERS:
        raise ReferenceDataError("normalized route does not start near Georgetown")
    if _distance(western, CUMBERLAND) > ENDPOINT_TOLERANCE_METERS:
        raise ReferenceDataError("normalized route does not end near Cumberland")
    return geometry


def _canonical_geojson(geometry: LineString, selected_count: int) -> FeatureCollection:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "direction": "Georgetown to Cumberland",
                    "name": "C&O Canal Towpath",
                    "selected_nps_feature_count": selected_count,
                    "source": "National Park Service Public Trails",
                    "unit_code": UNIT_CODE,
                },
                "geometry": mapping(geometry),
            }
        ],
    }


def _metadata(retrieved: date, source_count: int, selected_count: int) -> str:
    return f"""# C&O Canal towpath reference geometry

- **Source:** National Park Service Public Trails dataset
- **FeatureServer:** {FEATURESERVER_URL}
- **Source filter:** `UNITCODE = 'CHOH'`
- **Source retrieved:** {retrieved.isoformat()}
- **Source CHOH features:** {source_count}
- **Selected towpath features:** {selected_count}

Canawler selects records with the NPS towpath mile-segment naming convention and expected unrestricted, public, existing, extant, pedestrian-and-bicycle trail attributes. It also explicitly recognizes the NPS `Widewater Waste Weir Bridge 3100-059S` record. Unrecognized Towpath-labeled or Towpath-named records make the build fail instead of being guessed into the route.

Processing converts the server's GeoJSON response to a single WGS84 (EPSG:4326) LineString, removes exact duplicate source lines, joins disconnected source chains only across gaps of 50 metres or less, extracts the unique endpoint-to-endpoint route, and orients it from Georgetown, Washington, DC, to Cumberland, Maryland. Coordinates are not simplified and the geometric length is not forced to the canal's 184.5-mile milepost value.

`towpath.geojson` is derived data intended for Canawler analysis, not navigation. The National Park Service is acknowledged as the source of the underlying trail data.
"""


def _json_text(payload: FeatureCollection) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def validate_canonical_reference(path: Path = DEFAULT_OUTPUT) -> LineString:
    """Validate a committed canonical reference without contacting NPS."""
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        features = payload["features"]
        if payload.get("type") != "FeatureCollection" or len(features) != 1:
            raise ReferenceDataError(
                "canonical reference must contain exactly one GeoJSON feature"
            )
        feature = features[0]
        geometry = shape(feature["geometry"])
        properties = feature.get("properties", {})
    except ReferenceDataError:
        raise
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ReferenceDataError(
            f"could not read canonical reference {path}: {error}"
        ) from error

    if (
        geometry.geom_type != "LineString"
        or geometry.is_empty
        or not geometry.is_valid
        or not geometry.is_simple
    ):
        raise ReferenceDataError(
            "canonical reference must be one valid, simple WGS84 LineString"
        )
    for longitude, latitude, *_ in geometry.coords:
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            raise ReferenceDataError(
                "canonical reference contains coordinates outside WGS84 bounds"
            )

    if not isinstance(properties, dict):
        raise ReferenceDataError("canonical reference properties must be an object")
    if properties.get("name") != "C&O Canal Towpath":
        raise ReferenceDataError("canonical reference name must be C&O Canal Towpath")
    if properties.get("direction") != "Georgetown to Cumberland":
        raise ReferenceDataError(
            "canonical reference direction must be Georgetown to Cumberland"
        )
    if properties.get("unit_code") != "CHOH":
        raise ReferenceDataError("canonical reference unit_code must be CHOH")

    eastern = tuple(geometry.coords[0])
    western = tuple(geometry.coords[-1])
    if _distance(eastern, GEORGETOWN) > ENDPOINT_TOLERANCE_METERS:
        raise ReferenceDataError("canonical towpath does not start near Georgetown")
    if _distance(western, CUMBERLAND) > ENDPOINT_TOLERANCE_METERS:
        raise ReferenceDataError("canonical towpath does not end near Cumberland")
    return geometry


def build_reference(
    source: FeatureCollection,
    output_path: Path = DEFAULT_OUTPUT,
    metadata_path: Path = DEFAULT_METADATA,
    retrieved: date | None = None,
) -> BuildReport:
    """Build, validate, and write deterministic canonical reference artifacts."""
    choh = filter_choh_features(source.get("features", []))
    _validate_schema(choh)
    selected = select_towpath_features(choh)
    geometry = normalize_towpath_geometry(selected)
    canonical = _canonical_geojson(geometry, len(selected))
    geojson_text = _json_text(canonical)

    output_path = Path(output_path)
    metadata_path = Path(metadata_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    unchanged = (
        output_path.exists() and output_path.read_text(encoding="utf-8") == geojson_text
    )
    if not unchanged:
        output_path.write_text(geojson_text, encoding="utf-8")
    if not unchanged or not metadata_path.exists():
        metadata_path.write_text(
            _metadata(
                retrieved or datetime.now(tz=UTC).date(),
                len(choh),
                len(selected),
            ),
            encoding="utf-8",
        )

    eastern = tuple(geometry.coords[0])
    western = tuple(geometry.coords[-1])
    return BuildReport(
        source_features=len(choh),
        selected_features=len(selected),
        geometry_type=geometry.geom_type,
        eastern_endpoint=eastern,
        western_endpoint=western,
        length_miles=abs(_GEOD.geometry_length(geometry)) / METERS_PER_MILE,
        is_valid=geometry.is_valid,
        output_path=output_path,
    )


__all__ = [
    "PUBLIC_CSV_DIR",
    "PUBLIC_DIR",
    "PUBLIC_JSON_DIR",
    "BuildReport",
    "ReferenceDataError",
    "build_reference",
    "fetch_choh_geojson",
    "filter_choh_features",
    "inspect_reference",
    "normalize_towpath_geometry",
    "public_format_directories",
    "select_towpath_features",
    "validate_canonical_reference",
]
