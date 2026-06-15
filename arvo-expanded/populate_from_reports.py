#!/usr/bin/env python3
"""Populate arvo-style DB columns by scraping OSS-Fuzz report pages.

For every localId in the target DB, fetch its issues.oss-fuzz.com report page,
parse the structured crash report embedded in the HTML, and fill these columns:
    project, fuzz_target, crash_type, sanitizer, fuzz_engine, severity, language

`language` comes from the project's OSS-Fuzz project.yaml (fetched once per
project, cached); `severity` from the Buganizer S-field. Most columns are only
written when empty; `severity` is authoritative and overwrites.

Usage: ./populate_from_reports.py <target.db> [--limit N] [--delay SECONDS]

Written by claude
"""
import sys, re, sqlite3, time, urllib.request, urllib.error, warnings

# The escaped HTML contains "\/" sequences; unicode_escape handles them fine
# but emits a noisy DeprecationWarning. Silence just that.
warnings.filterwarnings("ignore", category=DeprecationWarning)

# Buganizer Severity enum (the sidebar S-field) -> arvo.db text vocabulary.
# Encoded in the issueState array as an int: 1=S0 .. 5=S4. Has fuller coverage
# than the "Recommended Security Severity" line in the report body, so it is the
# authoritative source. A miss is written as the raw S-code (and warned about).
SEVERITY_INT_MAP = {1: "Critical", 2: "High", 3: "Medium", 4: "Low", 5: "Low"}
SEVERITY_CODE = {1: "S0", 2: "S1", 3: "S2", 4: "S3", 5: "S4"}

# Exact OSS-Fuzz sanitizer label -> arvo.db short form. A miss is written
# verbatim (and warned about), per requirement.
SANITIZER_MAP = {
    "address (ASAN)": "asan",
    "memory (MSAN)": "msan",
    "undefined (UBSAN)": "ubsan",
}

REPORT_URL = "https://issues.oss-fuzz.com/issues/{}"
PROJECT_YAML_URL = (
    "https://raw.githubusercontent.com/google/oss-fuzz/master/projects/{}/project.yaml"
)

# arvo.db only covers C/C++. The OSS-Fuzz project.yaml `language` field is the
# authoritative source; anything outside this map is warned about and left empty.
LANGUAGE_MAP = {"c": "c", "c++": "c++"}
_LANG_CACHE = {}  # project -> language (or None); fetched once per run


def fetch(local_id):
    req = urllib.request.Request(
        REPORT_URL.format(local_id), headers={"User-Agent": "Mozilla/5.0"}
    )
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")


def parse(html, local_id):
    """Return parsed fields, or None if the report body can't be found."""
    decoded = html.encode().decode("unicode_escape", errors="replace")
    m = re.search(r"Detailed Report:.*?Issue filed automatically\.", decoded, re.S)
    if not m:
        return None
    body = m.group(0)

    def field(label):
        mm = re.search(rf"^{label}:[ \t]*(.+)$", body, re.M)
        return mm.group(1).strip() if mm else None

    # Buganizer Severity: the int immediately before the issue title in the
    # issueState array, e.g.  438321213,[1638179,6,5,3,<sev>,"leptonica:..."
    # Read from the raw HTML (not the de-escaped body).
    sm = re.search(rf"{local_id},\[\d+,\d+,\d+,\d+,(\d+),\"", html)
    severity_int = int(sm.group(1)) if sm else None

    return {
        "project": field("Project"),
        "fuzz_target": field("Fuzz Target"),
        "crash_type": field("Crash Type"),
        "fuzz_engine": field("Fuzzing Engine"),
        "sanitizer_raw": field("Sanitizer"),
        "severity_int": severity_int,
    }


def normalize_severity(sev_int, local_id):
    if sev_int is None:
        print(f"  WARNING [{local_id}]: severity field not found in page",
              file=sys.stderr)
        return None
    if sev_int in SEVERITY_INT_MAP:
        return SEVERITY_INT_MAP[sev_int]
    code = SEVERITY_CODE.get(sev_int, f"S?({sev_int})")
    print(f"  WARNING [{local_id}]: no severity mapping for int {sev_int}; "
          f"writing raw code {code}", file=sys.stderr)
    return code


def get_language(project, local_id):
    """Authoritative language from the project's OSS-Fuzz project.yaml.

    Fetched once per project per run (cached). Non-C/C++ or missing values are
    warned about and return None (column left empty).
    """
    if project in _LANG_CACHE:
        return _LANG_CACHE[project]

    lang = None
    try:
        req = urllib.request.Request(
            PROJECT_YAML_URL.format(project), headers={"User-Agent": "Mozilla/5.0"}
        )
        yaml = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
        mm = re.search(r"^language:[ \t]*([^\s#]+)", yaml, re.M)
        raw = mm.group(1).strip().strip("\"'").lower() if mm else None
        if raw is None:
            print(f"  WARNING [{local_id}]: no language in {project} project.yaml",
                  file=sys.stderr)
        elif raw in LANGUAGE_MAP:
            lang = LANGUAGE_MAP[raw]
        else:
            print(f"  WARNING [{local_id}]: non-C/C++ language {raw!r} for "
                  f"project {project}; leaving empty", file=sys.stderr)
    except urllib.error.URLError as e:
        print(f"  WARNING [{local_id}]: project.yaml fetch failed for "
              f"{project}: {e}", file=sys.stderr)

    _LANG_CACHE[project] = lang
    return lang


def normalize_sanitizer(raw, local_id):
    if raw is None:
        return None
    if raw in SANITIZER_MAP:
        return SANITIZER_MAP[raw]
    print(
        f"  WARNING [{local_id}]: no sanitizer mapping for {raw!r}; "
        f"writing value verbatim",
        file=sys.stderr,
    )
    return raw


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: populate_from_reports.py <target.db> [--limit N] [--delay SECONDS]")
    db_path = sys.argv[1]
    limit = None
    delay = 0.5
    args = sys.argv[2:]
    for i, a in enumerate(args):
        if a == "--limit":
            limit = int(args[i + 1])
        elif a == "--delay":
            delay = float(args[i + 1])

    conn = sqlite3.connect(db_path)
    ids = [r[0] for r in conn.execute("SELECT localId FROM arvo ORDER BY localId")]
    if limit is not None:
        ids = ids[:limit]

    total = len(ids)
    updated = failed = 0
    print(f"Processing {total} reports from {db_path}", file=sys.stderr)

    for n, local_id in enumerate(ids, 1):
        try:
            data = parse(fetch(local_id), local_id)
        except urllib.error.URLError as e:
            print(f"  WARNING [{local_id}]: fetch failed: {e}", file=sys.stderr)
            failed += 1
            continue
        if not data:
            print(f"  WARNING [{local_id}]: no report body found", file=sys.stderr)
            failed += 1
            continue

        sanitizer = normalize_sanitizer(data["sanitizer_raw"], local_id)
        engine = data["fuzz_engine"].lower() if data["fuzz_engine"] else None
        severity = normalize_severity(data["severity_int"], local_id)
        language = get_language(data["project"], local_id) if data["project"] else None

        # COALESCE columns fill only when empty; severity is authoritative from
        # the Buganizer S-field, so it overwrites (consistent value on re-run).
        conn.execute(
            """UPDATE arvo SET
                 project     = CASE WHEN project IS NULL OR project='' THEN ? ELSE project END,
                 fuzz_target = COALESCE(fuzz_target, ?),
                 crash_type  = COALESCE(crash_type, ?),
                 sanitizer   = COALESCE(sanitizer, ?),
                 fuzz_engine = COALESCE(fuzz_engine, ?),
                 severity    = COALESCE(?, severity),
                 language    = COALESCE(language, ?)
               WHERE localId = ?""",
            (data["project"], data["fuzz_target"], data["crash_type"],
             sanitizer, engine, severity, language, local_id),
        )
        conn.commit()
        updated += 1
        print(f"  [{n}/{total}] {local_id}: {data['project']} / "
              f"{data['crash_type']} / {sanitizer} / sev={severity} / {language}",
              file=sys.stderr)
        time.sleep(delay)

    conn.close()
    print(f"Done: {updated} updated, {failed} failed, {total} total", file=sys.stderr)


if __name__ == "__main__":
    main()
