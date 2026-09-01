"""Mirror public data artifacts into the site."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


class PublishError(RuntimeError):
    """Raised when the site cannot be prepared for publishing."""


def repository_root() -> Path:
    return Path(__file__).resolve().parent.parent


def publish_public_data(root: Path) -> int:
    source = root / "data" / "public"
    destination = root / "site" / "data" / "public"

    if not source.is_dir():
        raise PublishError(
            "Missing data/public/. "
            "Run `uv run canawler build` before publishing the site."
        )

    public_files = [path for path in source.rglob("*") if path.is_file()]
    if not public_files:
        raise PublishError(
            "data/public/ contains no files. "
            "Run `uv run canawler build` before publishing the site."
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
    root = repository_root()
    print("Publishing public data", flush=True)
    file_count = publish_public_data(root)
    print(f"Public artifacts: {file_count} files", flush=True)
    print("Copied data/public -> site/data/public", flush=True)
    print("Rendering site with Quarto", flush=True)
    try:
        subprocess.run(["quarto", "render", "site"], cwd=root, check=True)
    except FileNotFoundError as error:
        raise PublishError("Quarto is not installed or is not on PATH.") from error
    except subprocess.CalledProcessError as error:
        raise PublishError(
            f"Quarto render failed with exit code {error.returncode}."
        ) from error


if __name__ == "__main__":
    try:
        main()
    except PublishError as error:
        raise SystemExit(str(error)) from None
