#!/bin/sh
set -eu

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repository_root=$(dirname "$script_directory")

mkdir -p "$repository_root/site/data/public"
cp "$repository_root/data/public/coverage.json" \
  "$repository_root/site/data/public/coverage.json"
cd "$repository_root"
quarto render site
