#!/bin/bash
# Resume the docs.alpaca.markets US mirror (docs/us/). Cloudflare rate-limits
# hard (~250 requests, then retry-after ~24 min), so this waits it out and
# fetches serially. Safe to re-run: it skips files already present.
set -u
cd "$(dirname "$0")/.."
sed 's|https://docs.alpaca.markets/us/||' docs/_urls.txt | sort > /tmp/want.txt
(cd docs/us && find . -name '*.md' | sed 's|^\./||' | sort) > /tmp/have.txt
comm -23 /tmp/want.txt /tmp/have.txt | while read -r rel; do
  out="docs/us/$rel"; mkdir -p "$(dirname "$out")"
  code=$(curl -sL --max-time 45 -o "$out" -w '%{http_code}' "https://docs.alpaca.markets/us/$rel")
  if [ "$code" != "200" ] || [ "$(wc -c < "$out")" -lt 200 ]; then
    rm -f "$out"
    ra=$(curl -sI "https://docs.alpaca.markets/us/$rel" | awk -F': ' 'tolower($1)=="retry-after"{print $2+0}')
    echo "throttled on $rel (http $code); sleeping ${ra:-900}s"
    sleep "${ra:-900}"
  fi
  sleep 0.5
done
echo "done: $(find docs/us -name '*.md' | wc -l) files"
