#!/bin/bash
# Stage an install tree from root/ for one package, or for all of them.
#   packaging/stage.sh <package|all> <destdir>
# Files and modes come from packaging/files.txt; the core package also
# carries the license, docs and examples.
set -euo pipefail
[ "$#" -eq 2 ] || { echo "usage: stage.sh <package|all> <destdir>" >&2; exit 2; }
want=$1
dest=$2
top=$(cd "$(dirname "$0")/.." && pwd)

grep -v '^#' "$top/packaging/files.txt" | while read -r package mode path; do
  [ -n "$package" ] || continue
  [ "$want" = all ] || [ "$want" = "$package" ] || continue
  if [ "$mode" = link ]; then
    mkdir -p "$(dirname "$dest$path")"
    ln -sfn "$(readlink "$top/root$path")" "$dest$path"
  else
    install -D -m "$mode" "$top/root$path" "$dest$path"
  fi
done
if [ "$want" = all ] || [ "$want" = claude-supervisor ]; then
  install -D -m 0644 "$top/LICENSE" "$dest/usr/share/licenses/claude-supervisor/LICENSE"
  install -D -m 0644 -t "$dest/usr/share/doc/claude-supervisor" "$top/README.md" "$top/CHANGELOG.md"
  install -D -m 0644 -t "$dest/usr/share/doc/claude-supervisor/examples" "$top"/examples/*
fi
