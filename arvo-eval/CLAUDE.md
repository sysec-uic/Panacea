# arvo-eval — Claude Code Context

## What this project is

A heuristic learning experiment on 30 mruby ARVO bugs. The core question: does
injecting a playbook of lessons extracted from earlier bugs help Claude Code fix
later ones faster/more often?

**Two passes, same 30 bugs, same chronological order:**
- **Control** — no playbook injected (baseline)
- **Treatment** — playbook injected as `HEURISTICS.md` into the agent's working dir

After each solved bug in treatment, an LLM extracts a heuristic and appends it to
the playbook. The holdout is chronological — a bug never sees its own lesson.

---

## Key files

| File | Role |
|------|------|
| `learn_loop.py` | Orchestrates control/treatment passes; reads/writes ledger; calls oracle |
| `repair_loop.py` | Per-bug retry loop (up to 5 attempts); passes deployment-faithful feedback between attempts |
| `arvo_oss_crs.py` | Wraps OSS-CRS to run Claude Code on an ARVO bug; copies `HEURISTICS.md` into agent workspace |
| `differential_oracle.py` | Grades a patch by comparing agent container vs `n132/arvo:{id}-fix` image |
| `verify_fix.py` | Deployment-faithful verifier: applies patch, compiles, runs PoC + `rake test` |
| `playbook_store.py` | Load/save/render the JSON playbook state |
| `injector.py` | Writes `HEURISTICS.md` into the per-bug project dir |
| `extract_heuristic.py` | LLM call to extract a plain success lesson |
| `contrastive_extract.py` | LLM call to extract a Don't/Do contrastive lesson (failed-then-succeeded) |
| `ledger.py` | Append/read `results/learn/ledger.<pass>.jsonl` |
| `llm.py` | LLM backend: prefers Claude Code CLI (`claude -p`), falls back to API key or local model |
| `mruby_bugs.py` | Returns the 30 mruby bug IDs in chronological (localId) order from `arvo_new.db` |

---

## Running the experiment

```bash
# Prerequisites each session
export CLAUDE_CODE_OAUTH_TOKEN=$(python3 -c "import json; d=json.load(open('$HOME/.claude/.credentials.json')); print(d['claudeAiOauth']['accessToken'])")
export OSS_CRS_COMPOSE_FILE=$HOME/oss-crs/example/crs-claude-code/compose-oauth.yaml

# Control pass (run first, no injection)
ARVO_DB_PATH=arvo_new.db LEARN_PASS=control .venv/bin/python3 learn_loop.py

# Treatment pass (run after control)
ARVO_DB_PATH=arvo_new.db LEARN_PASS=treatment .venv/bin/python3 learn_loop.py
```

The loop **resumes automatically** — it reads the ledger on startup and skips any
bug already recorded for the current pass.

**Target specific bugs** (useful to skip problematic ones):
```bash
ARVO_DB_PATH=arvo_new.db LEARN_PASS=control .venv/bin/python3 learn_loop.py --bugs 445470271,446362556
```

---

## Current experiment state (as of 2026-07-12)

- **Control:** 13/30 done, all `oracle_confirmed`, 100% fix rate
- **Treatment:** 10/30 done, all `oracle_confirmed`, 100% fix rate so far
- Bug `444775186` — previously flagged as reliably triggering Anthropic cyber
  safeguards (agent tries to analyze crash via truncated PoC inputs, gets blocked).
  Retried on treatment 2026-07-14 with playbook_version=9 and came back
  `verified_correct` on the first attempt, no safeguard block. No longer treating
  this as a hard skip — worth also retrying on control. Note it's absent from
  control's ledger too (never actually skipped-and-recorded, just never run).
- **Skipping bug `448044860`** — reliably stalls. It's the same bigint pool-escape bug
  class as heuristic `h-439279102`, but the numerically-correct fix exposes a real
  infinite-loop regression in `mpz_mod`/`div_2exp`; the agent finds this, reverts, and
  runs out of the 1800s `AGENT_TIMEOUT` before finalizing a clean patch. Two attempts
  both came back with 0 patches and no ledger entry. Revisit with a longer
  `AGENT_TIMEOUT` or omit.
- Stale result.json files under `results/control/{454148440,455605217,455612769,456317307}`
  (5-10s elapsed, 0 pov_runs) are leftovers from a June 30/July 1 run with a broken
  `OSS_CRS_COMPOSE_FILE`/missing OAuth token — not evidence those bugs are hard. Don't
  skip them; they just need a normal re-run now that the env vars are fixed.
- **Skipping bug `449429295`** — its `build-target` docker build deterministically fails
  (8/8 identical retries) on `apt-get update` inside `builder.Dockerfile`: a WSL2/Docker
  MTU mismatch (host `eth0` MTU 1440 vs `docker0` bridge MTU 1500) truncates the
  `InRelease` download. Fixable with `sudo iptables -t mangle -A POSTROUTING -p tcp
  --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu`, but declined for now (host-level
  change). This is environment-level, not bug-specific — could in principle recur on any
  bug that needs a fresh docker image layer build, though other bugs have built fine so far.
  Note: `run_oss_crs`'s `build-target` subprocess call runs with `check=True`, so a
  failed build raises. This used to crash the whole `learn_loop.py` process mid-batch
  (as happened 2026-07-12). As of the 2026-07-17 crash-isolation fix, `run_pass` wraps
  each bug in try/except: a raising agent/verify/grade is now recorded as an `error`
  verdict in the ledger (so resume skips it) and the campaign continues to the next bug
  instead of dying. A reliably-crashing bug like this one still won't get fixed, but it
  no longer takes the rest of the batch down with it.
- **Skipping bug `449498801`** — its ARVO `-vul` image (`n132/arvo:449498801-vul`) hits a
  corrupted containerd overlay snapshot (`failed to stat parent: ... snapshots/2648/fs: no
  such file or directory`), unrelated to the MTU issue above. Restarting the docker daemon
  and `docker rmi`+re-pulling the image didn't fix it — the broken snapshot is a shared
  parent/base layer, and digest-matched re-pulls skip re-downloading it. A snapshot-count
  check (373 dirs vs 375 containerd metadata entries) showed the corruption is small/
  isolated, not systemic, so a full `/var/lib/docker` + `/var/lib/containerd` wipe was
  judged not worth it — it would also evict all cached build layers, exposing every future
  bug's build-target step to the still-unresolved MTU apt-failure risk. Just skip this bug.

---

## To-do (as of 2026-07-20)

Priority order, per explicit decisions — don't reorder without checking in:

1. ~~Reconcile `feature/live-status-ui` with `origin/main`~~ — done 2026-07-22.
   `origin/main` had grown to 22 commits ahead (crash-orientation feature plus the
   two real fixes: per-bug crash isolation in `run_pass`, and check-patch
   auto-submit). Manually reintegrated the real `learn_loop.py` conflict (nested our
   attempt-level checkpointing/resume + usage-limit/abort handling inside their
   try/except crash isolation, rather than picking a side); `arvo_oss_crs.py`'s
   conflict was the anticipated trivial dict-key collision. Two pre-existing tests
   (`test_attempt_level_resume_continues_after_simulated_crash`,
   `test_checkpoint_path_for_none_disables_resume_entirely`) broke because they
   simulated "process died mid-attempt" by raising a plain `Exception`, which the
   new crash isolation now legitimately swallows — fixed by switching those
   simulated crashes to `BaseException`, since a genuine whole-process death
   (OOM/SIGKILL/usage-cap) would never reach `run_pass`'s `except Exception` in the
   first place. Full suite (287 tests) passed before push.
2. ~~Wire `verify_fix.py`/`differential_oracle.py` into the live status UI as real
   phases~~ — done 2026-07-22. `PHASE_ORDER`/`PHASE_LABELS` in `learn_loop.py`
   extended from 3 to 5 entries; new `_make_verify`/`_make_grade` wrappers bracket
   the existing `_default_verify`/`_default_grade` calls with `on_phase(key,
   "start"/"done")`, mirroring `_make_agent`. Neither `verify_fix.py` nor
   `differential_oracle.py` was touched — they stay pure Docker-only functions with
   no UI awareness. "verify fix" only activates on attempts with a real diff to
   check; "differential oracle" only activates once per bug, after a solve.
3. ~~Merge teammate's `fix/verify-pipeline-e2e` follow-on commits~~ — done 2026-07-23.
   Found on `origin/fix/verify-pipeline-e2e` (not yet in `origin/main`): a per-pass
   working-dir/ledger split (`bug_workdir`, `ledger.<pass>.jsonl`) and, more
   importantly, a real bug fix — `find_target_source_dir` used global max-mtime to
   pick the agent's source dir, which the never-cleaned OSS-CRS workdir made
   unreliable once other builds/check-patch rebuilds touched other dirs; a
   treatment run could silently get no `HEURISTICS.md` and run as if it were
   control, with no error. Fixed by pinning to a before-build snapshot + build-start
   timestamp instead of mtime. Audited our own on-disk treatment logs before
   merging: grepped every `oss_crs_claude_stdout.log` we have for `HEURISTICS`
   mentions in the agent's own transcript — all 22 show at least 2, so none of our
   historical treatment runs appear to have been silently degraded. Merge conflicts
   in `arvo_oss_crs.py`/`learn_loop.py` were both in the `build-target` region (kept
   our `_log`/`_phase`/`_run_agent_with_timeout` UI wrapping, added their
   ts_before/build_start snapshot around it); fixed one merge-introduced bug where
   `ledger_path` was left as a bare undefined name inside `main()`'s live-UI branch.
   Migrated our existing `results/learn/ledger.jsonl` (20 control + 21 treatment) into
   `ledger.control.jsonl`/`ledger.treatment.jsonl` by the existing `pass` field: old
   file backed up, not deleted. 294 tests pass.
4. **`detect_cyber_refusal`** — mirror `detect_usage_limit` (`arvo_oss_crs.py:284`),
   surface Anthropic cyber-safeguard refusals as their own ledger field instead of
   silently blending into `no_changes`. Motivated by `462331852` (2026-07-20): its
   first control attempts got refused mid-cleanup *after* already root-causing the bug
   and building a working patch — the ledger currently can't distinguish that from a
   genuine dead end.
5. **Hoist `maybe_compress` out of the per-attempt retry loop** — `learn_loop.py`'s
   `attempt_agent` closure recomputes the compression LLM call fresh on every retry of
   a bug, even though the playbook state doesn't change across a bug's attempts. Waste
   that compounds on hard bugs burning multiple retries. Also causes a visible UI gap
   (noticed 2026-07-22 on a fresh treatment run of `472003599`): `attempt_agent` calls
   `maybe_compress(render_playbook(...))` on line 285, BEFORE calling `agent()` on line
   289 — and it's `agent()` (via `_make_agent`'s `before_attempt` wrapper) that resets
   the phase tracker / sets subject+position+tallies+stats for this bug. So on treatment
   (where the playbook is now well past the 3000-char compression cap and every attempt
   pays for a real LLM call), the panel sits blank for that call's duration before the
   bug's phases even appear — most visible on the very first bug of a run. Worth adding
   a "preparing playbook" phase around this call when the hoist happens.
6. **Possible `usage_exhausted` ledger classification** — for bugs whose attempts all
   get cut off by the session usage limit instead of reaching a real `no_changes`
   verdict (e.g. `455612769`, 2026-07-21). Not the same as a genuine dead end — still
   useful data, shouldn't undercount fix rate.
7. **Possible fix: `repair_loop.py:94` checks `usage_limit` before checking for a
   diff** — on `455612769` (treatment, 2026-07-21) a rate-limit rejection hit ~28min
   into the run, the agent kept going on overage and submitted a real patch 23min
   later, but the whole attempt still got discarded as `"interrupted"` (no verify, no
   ledger entry) — had to be manually recovered. Check for a non-empty diff first.
8. **Report verify/oracle progress into the live status panel's raw feed** (lower
   priority, added 2026-07-22 as a follow-on to #2 above) — build/agent already
   stream real subprocess output into the `v`-toggled raw panel via `on_line`, but
   verify and the oracle currently give zero insight beyond a spinner + elapsed
   time once their phase goes active: `verify_fix.run_check` and
   `differential_oracle.grade` are blocking `subprocess.run`/`docker_exec` calls
   with no callback hook at all. Plan: add an optional `on_step(msg)` param
   (default `None`, so every existing caller — CLI entry points, check-patch,
   tests — is unaffected) to both, firing a short message before each already-
   identifiable step (verify: apply patch → compile → run PoC → run `rake test`;
   oracle: build agent container → start fix container → run PoC → check binary →
   build/read probe goldens → run each of the 6 probes, "probe N/6"). Thread
   `status.feed_raw` through as that `on_step` in `_make_verify`/`_make_grade`.
   Unlike #2, this DOES require touching `verify_fix.py`/`differential_oracle.py`.
9. **Archive each attempt's run logs before the next overwrites them** — found
   2026-07-22 while comparing control vs treatment on `472003599`: control took 3
   attempts, but `results/control/472003599/` (`oss_crs_claude_stdout.log`,
   `oss_crs_patch_*.diff`, `oss_crs_result.json`, `patch.diff`, `verification.json`)
   is the SAME fixed path every attempt (`_agent_results_dir(bug_id)` in
   `learn_loop.py`, matched independently by `verify_fix.results_dir()`), so only
   attempt 3's files survived — attempts 1-2's logs/patches/verification are gone,
   nothing to inspect after the fact. Plan: archive into
   `results/<pass>/<bug_id>/attempt_logs/attempt_<n>/` from inside `run_pass`'s
   `on_attempt` callback (repair_with_retries only calls this for a REAL,
   checkpointed attempt — never one cut short by a usage cap or abort — so
   `record["attempt"]` is always a genuine number and every archived folder
   corresponds to an actual repair_loop record). Also write that `record` itself as
   `record.json` alongside the copied files, since `verify_fix.verify()` is skipped
   entirely for a no-diff attempt and would otherwise leave a stale
   `verification.json` in the archive. No changes needed to `arvo_oss_crs.py` or
   `verify_fix.py` — pure archiving glue in `learn_loop.py`, same spirit as
   `_make_verify`/`_make_grade`. Applies to every run (control + treatment, live UI
   or not) — this is a retry-mechanics gap, not something specific to the panel.
10. ~~`llm.py`'s retry logic never retries the CLI backend~~ — done 2026-07-23.
   `with_retries`/`_is_retriable` only recognized a failure as retriable via
   `exc.status_code`, which only exists on raw `anthropic` SDK exceptions.
   `ClaudeCLIClient` raised plain `RuntimeError`/`subprocess.TimeoutExpired` with no
   `status_code` at all, so none of ITS failures ever got retried, no matter how
   transient — bit us twice (`472003599`: `is_error: true` with no explanatory code;
   `472140765`: a 600s CLI hang), both crashing `maybe_compress` before the agent
   ever ran (see #5 — it's called before `agent()`/`before_attempt`) and needing a
   manual ledger cleanup + re-run each time. Fix: added `CLICallError(RuntimeError)`,
   changed `ClaudeCLIClient.create()`'s three failure sites to raise it instead of
   plain `RuntimeError`, and extended `_is_retriable` to treat `CLICallError` and
   `subprocess.TimeoutExpired` as retriable alongside the existing status-code check.
   `CLICallError` subclasses `RuntimeError` so no existing `except RuntimeError`
   caller needed to change. Also made the retry itself observable: `with_retries`
   previously only printed on the 429 branch, so this exact class of retry would
   have fired completely silently — added a matching print (exception type +
   attempt + backoff) to the other branch too, same stderr/no-callback design as
   the 429 print (curator/extractor callers have no live-UI awareness today, so
   this carries the same, already-accepted stdout-during-live-panel risk as that
   429 print always has — not a new risk class; properly routing it through
   `status.feed_raw` would mean threading an `on_retry` callback through
   `curator.py`/`extract_heuristic.py`/`contrastive_extract.py`, real scope that
   overlaps with #8). Scoped to our own `claude_cli` backend only — teammate's
   local-model runs select the `openai` backend whenever `OPENAI_BASE_URL` is set,
   so `ClaudeCLIClient`/`CLICallError` never enter that path. 5 new tests (299
   total pass).

**Declined for now, to avoid disturbing the running experiment:** raising the
3000-char playbook compression cap, and the retrieval-based playbook redesign
(inject only heuristics relevant to the current bug instead of compressing the whole
thing). Revisit after the current 30-bug run completes, not mid-experiment.

**Low priority, revisit later:** `AbortController.requested` (a single
`threading.Event()` per `learn_loop.py` process, never reset between attempts/bugs —
if a real abort ever fires, every later attempt in that process silently self-aborts).
Not causing active harm; not decided whether it needs fixing.

**Unconfirmed, worth asking about:** teammate may have added a separate heuristic-
learning setup that injects scripts rather than a markdown playbook — possibly worth
comparing against the same ~20-bug set this experiment settles on. Not yet understood
well enough to act on; get details before assuming it's related to the check-patch
auto-submit feature in #1.

---

## Ledger

`results/learn/ledger.control.jsonl` / `ledger.treatment.jsonl` — one JSON record per bug,
one file per pass. Git-ignored (local only).

Key fields:
- `classification`: `verified_correct` | `no_changes` | `still_crashes` | etc.
- `oracle_label`: `oracle_confirmed` | `divergent` | `oracle_error` | `no_fix_available`
- `n_attempts`: how many repair attempts (1 = first try success)
- `n_divergences`: number of probe/PoC differences vs fix image
- `tokens`: `{input_tokens, output_tokens, cache_read_tokens, cache_write_tokens}`

**Note on n_attempts:** Anthropic cyber safeguards can burn attempt slots (empty diff
→ retry). Treat `n_attempts` as directional, not a clean efficiency measure. Fix rate
(`verified_correct` %) is the cleaner primary metric.

---

## Oracle (`differential_oracle.py`)

Grades patches without showing the agent the fix image. Compares:
1. **PoC exit code** — agent-patched container vs fix image (exit code only; fuzzer
   stdout is non-deterministic noise)
2. **6 probe scripts** — `differential/mruby_probes/*.rb` run through `bin/mruby`;
   compares full stdout + exit code

**Probe goldens** (`differential/golden/{id}.json`) — cached `(exit, stdout)` from
the compiled fix container. Built once per bug on first grade, never recompiled again.
This is critical: the fix image (`n132/arvo:{id}-fix`) ships without a compiled
`bin/mruby`, so the oracle compiles it once and caches results.

Labels:
- `oracle_confirmed` — patch matches fix image on all probes + PoC
- `divergent` — passes tests but differs from fix image (still records `tests_only` lesson)
- `oracle_error` — oracle infra failure (never silences a lesson silently)
- `no_fix_available` — no `n132/arvo:{id}-fix` image exists

---

## Playbook

- `playbook/playbook_state_control.json` — loaded at the start of the control pass but
  never modified (control neither injects nor extracts). The 7 heuristics currently in it
  are stale artifacts from before commit `a2e9421` fixed a bug where control was
  incorrectly extracting heuristics. Safe to ignore.
- `playbook/playbook_state_treatment.json` — live treatment playbook; updated after each
  solved bug and injected as `HEURISTICS.md` into each subsequent bug's agent workspace

Rendered to `HEURISTICS.md` by `injector.py` into the fake OSS-Fuzz project dir.
Then copied into the OSS-CRS `target-source` dir by `arvo_oss_crs.inject_heuristics()`
— this happens after `build-target` (which creates `target-source`) but before `run`.

---

## Tests

```bash
PYTHONPATH=. .venv/bin/python3 -m pytest tests -q
```

138 tests, all pure logic (no Docker/network). Run before any changes to `learn_loop.py`,
`differential_oracle.py`, `repair_loop.py`, or `playbook_store.py`.

---

## Gotchas

- **OAuth token expires** — re-export before each session or runs silently complete in
  ~5 seconds with 0 LLM calls
- **OSS_CRS_COMPOSE_FILE** — must point to `compose-oauth.yaml` when using OAuth; the
  default is `compose-local.yaml` (local model tunnel)
- **`results/` is git-ignored** — ledger and per-bug outputs are local only; commit
  `playbook/` to share what the system learned
- **`arvo_new.db`** — required; download from the
  [ARVO_New release](https://github.com/sysec-uic/Panacea/releases/tag/ARVO_New_in_prog)
- **Bug order matters** — always use chronological order (`mruby_bug_ids(db)`) so the
  holdout is valid; `--bugs` flag accepts a comma-separated list in any order but the
  loop processes them in the order returned by `mruby_bug_ids`
- **`~/oss-crs/.oss-crs-workdir/` grows unbounded** — every `build-target`/`run`
  invocation leaves a full `builds/<id>/` and `runs/<id>/` tree under
  `crs_compose/<hash>/<sanitizer>/{builds,runs}/`, never cleaned up automatically.
  This is what filled the WSL2 VHDX to 268GB and dropped the Windows C: drive to
  172MB free on 2026-07-13 (see git history around that date). Much of the tree is
  root-owned (containers bind-mount in as root), so a plain `rm -rf` silently leaves
  most of it behind (1.4M+ files) with nothing louder than per-file "Permission
  denied" lines. **Use OSS-CRS's own cleanup command instead of manual `rm`/`sudo`**
  — it uses Docker itself to remove root-owned files, no sudo needed:
  ```bash
  cd ~/oss-crs
  uv run oss-crs clean --compose-file example/crs-claude-code/compose-oauth.yaml --artifacts --yes
  ```
  (`--artifacts` is required to also remove the `builds/`/`runs/` directories, not
  just leftover Docker images; omitting the subcommand after `clean` cleans all
  phases — prepare, build-target, and run.) Check
  `du -h --max-depth=1 ~/oss-crs/.oss-crs-workdir` periodically; nothing in
  `arvo-eval/results/` depends on this directory persisting. Freeing space inside
  WSL2 does NOT shrink the Windows-visible VHDX file by itself — that needs
  `wsl --shutdown` + `diskpart` → `select vdisk file=...` → `compact vdisk` run from
  a genuine Windows terminal afterward.
- **After wiping Docker images (e.g. `docker system prune -a`, or the disk-space
  recovery above), re-run `oss-crs prepare` before anything else** — `crs-claude-code`
  depends on a locally-built `claude-code-base:latest` image that is NOT pulled from
  a registry; if it's missing, every `build-target`/`run` invocation fails instantly
  (~7s) with `pull access denied, repository does not exist` for
  `docker.io/library/claude-code-base:latest`, and the loop burns all 5 retry
  attempts on this identical failure with `n_attempts: 5`, `no_changes`, and every
  token field at 0. That token/timing signature (near-zero elapsed, all-zero tokens,
  `n_attempts` maxed out, no `oss_crs_claude_stdout.log` written at all) is the
  tell that the agent never even started, same underlying category as the OAuth
  gotcha above but a different root cause — check the actual log for `pull access
  denied` before assuming the agent genuinely failed. Fix:
  ```bash
  cd ~/oss-crs
  uv run oss-crs prepare --compose-file example/crs-claude-code/compose-oauth.yaml
  ```
  This happened on 2026-07-13 after the WSL2 disk-space recovery wiped all Docker
  images; bug `439494108`'s first treatment-pass result was invalid because of this
  and had to be deleted from the ledger and re-run.
