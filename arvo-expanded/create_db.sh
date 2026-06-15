#!/usr/bin/env bash
# Create an empty ARVO-style SQLite database with the canonical `arvo` schema.
#
# The schema is byte-for-byte identical to arvo.db's `arvo` table, so the
# resulting DB is a drop-in for the same tooling. Populate it afterwards with
# insert_tags.sh.
#
# Usage: ./create_db.sh <target.db> [--force]
#   --force   overwrite the file if it already exists
#
# Written by claude
set -euo pipefail

DB="${1:?usage: create_db.sh <target.db> [--force]}"
FORCE="${2:-}"

if [ -e "$DB" ]; then
    if [ "$FORCE" = "--force" ]; then
        rm -f "$DB"
    else
        echo "error: $DB already exists (pass --force to overwrite)" >&2
        exit 1
    fi
fi

sqlite3 "$DB" "CREATE TABLE arvo (
            localId INTEGER PRIMARY KEY,
            project TEXT NOT NULL,
            reproduced BOOLEAN NOT NULL,
            reproducer_vul TEXT,
            reproducer_fix TEXT,
            patch_located BOOLEAN,
            patch_url TEXT,
            verified BOOLEAN,
            fuzz_target TEXT,
            fuzz_engine TEXT,
            sanitizer TEXT,
            crash_type TEXT,
            crash_output TEXT,
            severity TEXT,
            report TEXT,
            fix_commit TEXT,
            language TEXT
        , repo_addr TEXT DEFAULT NULL, submodule_bug BOOLEAN DEFAULT 0);"

echo "Created empty arvo database: $DB" >&2
