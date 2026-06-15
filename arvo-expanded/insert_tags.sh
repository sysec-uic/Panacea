#!/usr/bin/env bash
# Insert ARVO Docker-Hub tags from a JSONL file into an existing arvo-style DB.
#
# Reads {"name":"<localId>-vul"|"<localId>-fix", ...} lines, groups them by
# localId, and inserts one row per ID with the derivable fields filled in:
#   localId, reproducer_vul, reproducer_fix, report
# An ID is inserted as long as it has at least a -vul tag; reproducer_fix is
# left NULL when no matching -fix tag exists.
#
# Dedupe: rows are inserted with INSERT OR IGNORE, so any localId already in
# the table is left untouched (existing values are kept).
#
# The target table must already exist (same schema as arvo.db). This script
# does NOT create it.
#
# Usage: ./insert_tags.sh <tags.jsonl> <target.db>
#
# Written by claude
set -euo pipefail

JSONL="${1:?usage: insert_tags.sh <tags.jsonl> <target.db>}"
DB="${2:?usage: insert_tags.sh <tags.jsonl> <target.db>}"

[ -f "$JSONL" ] || { echo "error: no such file: $JSONL" >&2; exit 1; }
[ -f "$DB" ]    || { echo "error: no such db: $DB" >&2; exit 1; }

# IDs that have a -vul tag (the set we insert) and IDs that have a -fix tag.
vul_ids="$(jq -r '.name' "$JSONL" | sed -n 's/-vul$//p' | sort -u)"
fix_ids="$(jq -r '.name' "$JSONL" | sed -n 's/-fix$//p' | sort -u)"

before="$(sqlite3 "$DB" "SELECT COUNT(*) FROM arvo;")"

# Single-quotes are safe to inline here: localIds are numeric.
{
    echo "BEGIN;"
    while read -r id; do
        [ -n "$id" ] || continue
        if grep -qxF "$id" <<<"$fix_ids"; then
            fix="'docker run --rm -it n132/arvo:${id}-fix arvo'"
        else
            fix="NULL"
        fi
        printf "INSERT OR IGNORE INTO arvo (localId, project, reproduced, reproducer_vul, reproducer_fix, verified, report) VALUES (%s, '', 0, 'docker run --rm -it n132/arvo:%s-vul arvo', %s, 0, 'https://issues.oss-fuzz.com/issues/%s');\n" \
            "$id" "$id" "$fix" "$id"
    done <<<"$vul_ids"
    echo "COMMIT;"
} | sqlite3 "$DB"

after="$(sqlite3 "$DB" "SELECT COUNT(*) FROM arvo;")"
echo "Processed $(wc -l <<<"$vul_ids") ids: inserted $((after - before)) new, kept $((before)) existing ($DB)" >&2
