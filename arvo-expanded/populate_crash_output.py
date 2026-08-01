#!/usr/bin/env python3
"""Populate the crash_output column by running ARVO reproducer images.

For each localId whose crash_output is still empty, pull the n132/arvo
`<id>-vul` image, run it (`arvo`) with networking disabled, and capture the
combined stdout/stderr — that output IS the sanitizer crash log.

Images are ~5GB each, so by default each image is removed (docker rmi) right
after its output is captured to keep disk usage bounded. Pass --keep-images to
retain them.

Only empty crash_output cells are written; existing values are left untouched.

Usage: ./populate_crash_output.py <target.db> [--limit N] [--timeout SECONDS]
                                   [--keep-images]

Written by claude
"""
import sys, subprocess, sqlite3

IMAGE = "n132/arvo:{}-vul"
PULL_TIMEOUT = 1800  # images are multi-GB; allow up to 30 min to pull


def run_reproducer(local_id, timeout):
    """Return the container's crash output (str), or None on infrastructure error.

    The image is pulled in a separate step whose output is discarded, so the
    captured text is purely the container's stdout/stderr (the crash log) and
    not docker's pull progress.
    """
    image = IMAGE.format(local_id)

    # Pull first, separately — its progress output is intentionally not captured.
    try:
        pull = subprocess.run(["docker", "pull", image],
                              capture_output=True, text=True, timeout=PULL_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f"  WARNING [{local_id}]: pull timed out after {PULL_TIMEOUT}s",
              file=sys.stderr)
        return None
    if pull.returncode != 0:
        print(f"  WARNING [{local_id}]: pull failed: "
              f"{pull.stderr.strip().splitlines()[-1] if pull.stderr.strip() else 'unknown'}",
              file=sys.stderr)
        return None

    try:
        # --network none: the sandbox blocks veth setup, and the PoC is local.
        proc = subprocess.run(
            ["docker", "run", "--rm", "--network", "none", image, "arvo"],
            capture_output=True, text=True, timeout=timeout, errors="replace",
        )
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "")
        print(f"  WARNING [{local_id}]: run timed out after {timeout}s", file=sys.stderr)
        return out or None
    return (proc.stdout or "") + (proc.stderr or "")


def remove_image(local_id):
    subprocess.run(["docker", "image", "rm", "-f", IMAGE.format(local_id)],
                   capture_output=True, text=True)


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: populate_crash_output.py <target.db> [--limit N] "
                 "[--timeout SECONDS] [--keep-images]")
    db_path = sys.argv[1]
    limit = None
    timeout = 300
    keep = False
    args = sys.argv[2:]
    for i, a in enumerate(args):
        if a == "--limit":
            limit = int(args[i + 1])
        elif a == "--timeout":
            timeout = float(args[i + 1])
        elif a == "--keep-images":
            keep = True

    # timeout + busy_timeout so a concurrent reader (e.g. a progress query)
    # can't instantly fail a commit; WAL lets readers and the writer coexist.
    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA journal_mode=WAL")
    ids = [r[0] for r in conn.execute(
        "SELECT localId FROM arvo "
        "WHERE crash_output IS NULL OR crash_output='' ORDER BY localId")]
    if limit is not None:
        ids = ids[:limit]

    total = len(ids)
    captured = failed = 0
    print(f"{total} rows need crash_output in {db_path}", file=sys.stderr)

    for n, local_id in enumerate(ids, 1):
        print(f"  [{n}/{total}] {local_id}: running reproducer...", file=sys.stderr)
        try:
            output = run_reproducer(local_id, timeout)
        finally:
            # Guarantee the ~5GB image is removed even on exception/KeyboardInterrupt,
            # so an interrupted long run can never pile images onto the disk.
            if not keep:
                remove_image(local_id)

        if not output:
            print(f"  WARNING [{local_id}]: no output captured", file=sys.stderr)
            failed += 1
            continue

        # Fill only if still empty; never clobber an existing value.
        conn.execute(
            "UPDATE arvo SET crash_output = ? "
            "WHERE localId = ? AND (crash_output IS NULL OR crash_output='')",
            (output, local_id),
        )
        conn.commit()
        captured += 1
        summary = next((ln for ln in output.splitlines() if "ERROR" in ln or "SUMMARY" in ln),
                       output.splitlines()[-1] if output.splitlines() else "")
        print(f"      captured {len(output)} chars | {summary[:90]}", file=sys.stderr)

    conn.close()
    print(f"Done: {captured} captured, {failed} failed, {total} total", file=sys.stderr)


if __name__ == "__main__":
    main()
