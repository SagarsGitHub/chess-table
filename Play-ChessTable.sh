#!/usr/bin/env bash
# Daily physical-board launch with the authenticated Aina API.
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec bash "$script_dir/Start-ChessTable.sh" --physical --enable-aina "$@"
