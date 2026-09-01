"""Mirror the canonical public data product into the Quarto project."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path


class SyncError(RuntimeError):
    """Raised when public data cannot be mirrored into the site."""


def site_directory() -> Path:
    return Path(__file__).resolve().parent.parent


def sync_public_data(site: Path) -> int:
    source = site.parent / "data" / "public"
    destination = site / "data" / "public"

    if not source.is_dir():
        raise SyncError(
            "Missing data/public/. "
            "Run `uv run canawler build` before rendering the site."
        )

    public_files = [path for path in source.rglob("*") if path.is_file()]
    if not public_files:
        raise SyncError(
            "data/public/ contains no files. "
            "Run `uv run canawler build` before rendering the site."
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=destination.parent, prefix=".public-"
    ) as temporary_directory:
        staged = Path(temporary_directory) / "public"
        shutil.copytree(source, staged)
        if destination.exists():
            shutil.rmtree(destination)
        staged.replace(destination)

    return len(public_files)


def main() -> None:
    file_count = sync_public_data(site_directory())
    print(f"Mirrored {file_count} public artifacts into site/data/public", flush=True)


if __name__ == "__main__":
    try:
        main()
    except SyncError as error:
        raise SystemExit(str(error)) from None
