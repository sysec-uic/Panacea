#!/usr/bin/env bash
# Fetch all tags for n132/arvo from Docker Hub API -> CSV (name,datea
#
# Written by claude, edited by Luke
#
set -u

OUT="$(pwd)/arvo_tags_raw.jsonl"
URL="https://hub.docker.com/v2/repositories/n132/arvo/tags?page_size=100"
: > "$OUT"

page=1
while [ -n "$URL" ] && [ "$URL" != "null" ]; do
    resp=$(curl -s --retry 5 --retry-delay 3 --retry-all-errors "$URL")
    if ! echo "$resp" | jq -e '.results' > /dev/null 2>&1; then
        echo "Page $page failed, retrying in 10s..." >&2
        sleep 10
        continue
    fi
    echo "$resp" | jq -c '.results[] | {name: .name, date: .tag_last_pushed}' >> "$OUT"
    URL=$(echo "$resp" | jq -r '.next')
    echo "Fetched page $page ($(wc -l < "$OUT") tags so far)" >&2
    page=$((page + 1))
    sleep 0.3
done
echo "DONE: $(wc -l < "$OUT") tags total" >&2
