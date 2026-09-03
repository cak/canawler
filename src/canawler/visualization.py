"""Build static presentation artifacts from authoritative Canawler outputs."""

from __future__ import annotations

import math
from io import BytesIO
from pathlib import Path

import geopandas as gpd
import matplotlib as mpl
import polars as pl
from plotnine import (
    aes,
    coord_fixed,
    element_rect,
    geom_map,
    geom_point,
    geom_text,
    ggplot,
    theme,
    theme_void,
)
from plotnine.exceptions import PlotnineError
from pyproj.exceptions import ProjError

from canawler.coverage import (
    CANAL_MILES,
    MATCH_CRS,
    CoverageError,
    milepost_intervals_to_geometries,
)
from canawler.reference import PUBLIC_CSV_DIR, validate_canonical_reference

DEFAULT_REFERENCE_PATH = Path("data/reference/co-towpath/towpath.geojson")
DEFAULT_COVERAGE_PATH = PUBLIC_CSV_DIR / "coverage-segments.csv"
DEFAULT_OUTPUT_PATH = Path("assets/generated/canal-coverage.svg")
DEFAULT_PNG_OUTPUT_PATH = Path("assets/generated/canal-coverage.png")

REQUIRED_COVERAGE_COLUMNS = {
    "coverage_scope",
    "coverage_status",
    "start_milepost",
    "end_milepost",
    "miles",
}

CANVAS_WIDTH_INCHES = 14
CANVAS_HEIGHT_INCHES = 6
PNG_DPI = 240

BACKGROUND_COLOR = "#FFFFFF"
TEXT_COLOR = "#212529"
ROUTE_COLOR = "#C2CAD0"
COVERAGE_COLOR = "#2563EB"
FONT_FAMILY = "DejaVu Sans"

ROUTE_LINE_WIDTH = 0.9
COVERAGE_LINE_WIDTH = 1.75
ENDPOINT_MARKER_SIZE = 1.8
ENDPOINT_LABEL_SIZE = 10.5
LABEL_HORIZONTAL_OFFSET_FRACTION = 0.009
LABEL_VERTICAL_OFFSET_FRACTION = 0.012
HORIZONTAL_PADDING_FRACTION = 0.04
VERTICAL_PADDING_FRACTION = 0.045
SVG_HASH_SALT = "canawler-canal-coverage"


class VisualizationError(RuntimeError):
    """A static visualization cannot be produced from its authoritative inputs."""


def _load_towpath(path: Path) -> gpd.GeoDataFrame:
    towpath = validate_canonical_reference(path)
    frame = gpd.GeoDataFrame(
        {"name": ["C&O Canal Towpath"]},
        geometry=[towpath],
        crs="EPSG:4326",
    )
    try:
        projected = frame.to_crs(MATCH_CRS)
    except (ProjError, ValueError) as error:
        raise VisualizationError(
            f"could not project the canonical towpath to {MATCH_CRS}: {error}"
        ) from error
    if projected.geometry.iloc[0].is_empty or projected.geometry.iloc[0].length <= 0:
        raise VisualizationError("projected canonical towpath has no usable geometry")
    return projected


def _load_coverage(path: Path) -> pl.DataFrame:
    try:
        frame = pl.read_csv(path)
    except (OSError, pl.exceptions.PolarsError) as error:
        raise VisualizationError(
            f"could not read coverage segments from {path}: {error}"
        ) from error

    missing = sorted(REQUIRED_COVERAGE_COLUMNS - set(frame.columns))
    if missing:
        raise VisualizationError(
            "coverage segments are missing required columns: " + ", ".join(missing)
        )

    try:
        completed = (
            frame.filter(
                (pl.col("coverage_scope") == "combined")
                & (pl.col("coverage_status") == "completed")
            )
            .select(
                pl.col("start_milepost").cast(pl.Float64, strict=True),
                pl.col("end_milepost").cast(pl.Float64, strict=True),
                pl.col("miles").cast(pl.Float64, strict=True),
            )
            .sort("start_milepost")
        )
    except pl.exceptions.PolarsError as error:
        raise VisualizationError(
            f"coverage segment mileposts must be numeric: {error}"
        ) from error
    if completed.is_empty():
        raise VisualizationError(
            "coverage artifact contains no combined completed segments"
        )
    if completed.null_count().sum_horizontal().item() > 0:
        raise VisualizationError("completed coverage segment values cannot be null")

    previous_end: float | None = None
    for index, (start, end, miles) in enumerate(completed.iter_rows()):
        if not all(math.isfinite(value) for value in (start, end, miles)):
            raise VisualizationError(
                f"completed coverage interval {index} must contain finite values"
            )
        if not 0 <= start <= CANAL_MILES or not 0 <= end <= CANAL_MILES:
            raise VisualizationError(
                f"completed coverage interval {index} is outside 0-{CANAL_MILES} miles"
            )
        if start >= end:
            raise VisualizationError(
                f"completed coverage interval {index} start must be less than its end"
            )
        if previous_end is not None and start < previous_end:
            raise VisualizationError("completed coverage intervals overlap")
        if not math.isclose(miles, end - start, abs_tol=1e-8):
            raise VisualizationError(
                f"completed coverage interval {index} mileage does not match its endpoints"
            )
        previous_end = end
    return completed


def _build_covered_geometry(
    towpath: gpd.GeoDataFrame,
    coverage: pl.DataFrame,
) -> gpd.GeoDataFrame:
    intervals = tuple(coverage.select("start_milepost", "end_milepost").iter_rows())
    try:
        geometries = milepost_intervals_to_geometries(
            towpath.geometry.iloc[0], intervals
        )
    except CoverageError as error:
        raise VisualizationError(
            f"could not build covered route geometry: {error}"
        ) from error
    if not geometries:
        raise VisualizationError("no covered route geometry could be produced")
    return gpd.GeoDataFrame(
        {
            "start_milepost": coverage.get_column("start_milepost").to_list(),
            "end_milepost": coverage.get_column("end_milepost").to_list(),
        },
        geometry=list(geometries),
        crs=towpath.crs,
    )


def _build_endpoint_labels(towpath: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    line = towpath.geometry.iloc[0]
    georgetown = line.coords[0]
    cumberland = line.coords[-1]
    min_x, min_y, max_x, max_y = line.bounds
    width = max_x - min_x
    height = max_y - min_y
    label_x_offset = width * LABEL_HORIZONTAL_OFFSET_FRACTION
    label_y_offset = height * LABEL_VERTICAL_OFFSET_FRACTION

    return gpd.GeoDataFrame(
        {
            "name": ["Georgetown", "Cumberland"],
            "x": [georgetown[0], cumberland[0]],
            "y": [georgetown[1], cumberland[1]],
            "label_x": [
                georgetown[0] - label_x_offset,
                cumberland[0] + label_x_offset,
            ],
            "label_y": [
                georgetown[1] - label_y_offset,
                cumberland[1] + label_y_offset,
            ],
            "horizontal_alignment": ["right", "left"],
            "vertical_alignment": ["top", "bottom"],
        },
        geometry=gpd.points_from_xy(
            [georgetown[0], cumberland[0]],
            [georgetown[1], cumberland[1]],
            crs=towpath.crs,
        ),
        crs=towpath.crs,
    )


def _build_plot(
    towpath: gpd.GeoDataFrame,
    covered: gpd.GeoDataFrame,
) -> ggplot:
    endpoints = _build_endpoint_labels(towpath)
    min_x, min_y, max_x, max_y = towpath.total_bounds
    width = max_x - min_x
    height = max_y - min_y
    horizontal_padding = width * HORIZONTAL_PADDING_FRACTION
    vertical_padding = height * VERTICAL_PADDING_FRACTION
    return (
        ggplot()
        + geom_map(data=towpath, color=ROUTE_COLOR, size=ROUTE_LINE_WIDTH)
        + geom_map(data=covered, color=COVERAGE_COLOR, size=COVERAGE_LINE_WIDTH)
        + geom_point(
            data=endpoints,
            mapping=aes(x="x", y="y"),
            color=TEXT_COLOR,
            fill=BACKGROUND_COLOR,
            size=ENDPOINT_MARKER_SIZE,
            stroke=0.55,
        )
        + geom_text(
            data=endpoints,
            mapping=aes(
                x="label_x",
                y="label_y",
                label="name",
                ha="horizontal_alignment",
                va="vertical_alignment",
            ),
            color=TEXT_COLOR,
            family=FONT_FAMILY,
            size=ENDPOINT_LABEL_SIZE,
        )
        + coord_fixed(
            xlim=(min_x - horizontal_padding, max_x + horizontal_padding),
            ylim=(min_y - vertical_padding, max_y + vertical_padding),
            expand=False,
        )
        + theme_void(base_family=FONT_FAMILY)
        + theme(
            plot_background=element_rect(fill=BACKGROUND_COLOR, color=BACKGROUND_COLOR),
            panel_background=element_rect(
                fill=BACKGROUND_COLOR, color=BACKGROUND_COLOR
            ),
            plot_margin=0.02,
            figure_size=(CANVAS_WIDTH_INCHES, CANVAS_HEIGHT_INCHES),
            svg_usefonts=True,
        )
    )


def _render_plot(plot: ggplot, output_format: str) -> bytes:
    stream = BytesIO()
    options: dict[str, object] = {
        "format": output_format,
        "width": CANVAS_WIDTH_INCHES,
        "height": CANVAS_HEIGHT_INCHES,
        "units": "in",
        "verbose": False,
    }
    if output_format == "svg":
        options["metadata"] = {"Date": None}
    else:
        options.update(
            {
                "dpi": PNG_DPI,
                "metadata": {"Software": "Canawler"},
            }
        )
    with mpl.rc_context({"svg.hashsalt": SVG_HASH_SALT}):
        plot.save(stream, **options)
    return stream.getvalue()


def _write_if_changed(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file() or path.read_bytes() != contents:
        path.write_bytes(contents)


def build_coverage_map(
    *,
    reference_path: Path = DEFAULT_REFERENCE_PATH,
    coverage_path: Path = DEFAULT_COVERAGE_PATH,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    png_output_path: Path = DEFAULT_PNG_OUTPUT_PATH,
) -> tuple[Path, Path]:
    """Build the canonical SVG and convenience PNG coverage maps."""
    reference_path = Path(reference_path)
    coverage_path = Path(coverage_path)
    output_path = Path(output_path)
    png_output_path = Path(png_output_path)
    towpath = _load_towpath(reference_path)
    coverage = _load_coverage(coverage_path)
    covered = _build_covered_geometry(towpath, coverage)
    plot = _build_plot(towpath, covered)

    try:
        svg_contents = _render_plot(plot, "svg")
        png_contents = _render_plot(plot, "png")
        _write_if_changed(output_path, svg_contents)
        _write_if_changed(png_output_path, png_contents)
    except (OSError, PlotnineError, ValueError) as error:
        raise VisualizationError(
            f"could not render coverage map outputs: {error}"
        ) from error
    for path in (output_path, png_output_path):
        if not path.is_file() or path.stat().st_size == 0:
            raise VisualizationError(f"coverage map was not written to {path}")
    return output_path, png_output_path


__all__ = [
    "DEFAULT_COVERAGE_PATH",
    "DEFAULT_OUTPUT_PATH",
    "DEFAULT_PNG_OUTPUT_PATH",
    "DEFAULT_REFERENCE_PATH",
    "VisualizationError",
    "build_coverage_map",
]
