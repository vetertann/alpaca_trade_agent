#!/bin/bash
# Re-clone the upstream Alpaca reference repos into vendor/.
set -eu
cd "$(dirname "$0")/.."
mkdir -p vendor
for r in alpaca-skills cli alpaca-py alpaca-trade-api-js alpaca-mcp-server; do
  [ -d "vendor/$r" ] && { echo "skip $r"; continue; }
  git clone --depth 1 "https://github.com/alpacahq/$r.git" "vendor/$r"
done
