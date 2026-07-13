# Learn-loop local-model campaign — postmortem & next steps (Jul 10–13, 2026)

Campaign: `learn_loop.py --limit 3` (treatment pass) on mruby ARVO bugs, agent =
OSS-CRS crs-claude-code driving Qwen3-Coder-30B (llama.cpp on cc@192.5.86.157,
reached through an SSH tunnel). Killed Jul 13 ~00:40 after ~57.6 h on the first bug
(439237851) with zero ledger entries. The pipeline itself ended the campaign in
much better shape than it started; the blocker is model serving speed, not the
loop design.

## What the campaign proved

The retry/feedback design works end-to-end:

1. Attempt 3 produced a fresh patch that only rewrote the fuzz harness
   (`proto_to_ruby.h/.cpp`) — the new harness guard rejected it
   (`patch_touches_harness`) without spinning up Docker.
2. The rejection feedback ("the harness is scaffolding; fix the project source")
   redirected attempt 4 into `mrbgems/mruby-bigint/core/bigint.c` — the real home
   of the ASAN stack-use-after-return. Root-cause direction, no hand-holding.
3. Tunnel drops during a run no longer kill the campaign (preflight waits).

## The numbers

**Campaign totals** — Jul 10 15:05 → Jul 13 00:40 (~57.6 h). Bugs completed: 0 of 3.
Ledger entries: 0. Lessons learned: 0. Attempts on bug 439237851: 4 of a max 5.

### Per-attempt (bug 439237851, mruby_proto_fuzzer, ASAN stack-use-after-return in mrb_bint_mod)

| # | Window | Duration | Turns | Tool calls | Tool errors | Edits | Outcome |
|---|--------|----------|-------|-----------|-------------|-------|---------|
| 1 | Jul 10 15:06 → Jul 11 02:00 | 10.9 h | 15 | 7 (6 Read, 1 Bash) | 1 | none | No patch — agent died to the 01:27 tunnel drop. Verdict was computed on the stale Jul 8 patch (`patch_touches_harness`) — the stale-patch bug, since fixed (`d58f28d`). |
| 2 | Jul 11 02:00 → 03:05 | ~65 min build, then run died in **4 s** | — | — | — | none | CRS run crashed before any service started (empty compose logs; cause unrecovered). Verdict again from the stale patch. |
| 3 | Jul 11 03:06 → 12:40 | 9.6 h | 92 | 54 (18 Read, 20 Bash, 8 Edit, 1 Write, 1 EnterPlanMode, 6 task tools) | 4 | 8× `proto_to_ruby.cpp/.h` | Submitted a fresh, well-formed **4.3 KB harness-only patch** → rejected by the new guard (`patch_touches_harness`), legitimately this time. ~6.3 min/turn. |
| 4 | Jul 11 12:41 → killed Jul 13 00:40 | 36.0 h | 102 | 61 (14 Read, 40 Bash, 5 Edit, 2 Write) | 10 | 5× `bigint.c`, 2× `patch.diff` | Redirected by feedback into `mruby-bigint/core/bigint.c` (root-cause direction), but never submitted; drifted into "very defensive fix" flailing. ~21 min/turn average, decaying to ~80 min/turn as context grew. Killed manually. |

**Prior run for contrast (Jul 8, before any of the fixes):** 2 h 52 m, 42 turns,
21 tool calls (14 Bash, 5 Read, 1 Edit, 1 Write), 6 tool errors. Produced a
corrupt, hand-written, harness-only diff — recorded as `verified_correct` by the
stub verify in 1 attempt. That single bad datapoint motivated most of the fixes
below.

**Tokens:** 0 reported across every local-model run — llama.cpp/LiteLLM emits
all-zero `usage` on this path (confirmed server-side, not a parser bug; the
`message.id` counting fix works when usage is present).

### Tunnel drops (all `Read from remote host: Connection reset by peer`, server side)

| # | When | Uptime before drop |
|---|------|--------------------|
| 1 | Jul 11 01:27 | ~10.4 h |
| 2 | Jul 11 ~03:55 | ~2.5 h |
| 3 | Jul 11 16:15 | ~12.3 h |
| 4 | Jul 11 22:37 | ~6.4 h |
| 5 | Jul 12 11:09 | ~12.5 h |
| 6 | Jul 12 13:16 | ~2.1 h |
| 7 | Jul 13 ~00:15 | ~11 h |

All seven auto-restarted within ~2 minutes (Claude Code background-task
babysitting). Drop 1 killed attempt 1's agent mid-conversation (pre-keepalive
settings would have detected it even slower); after the keepalive flags landed,
no drop killed an agent run.

### Model server measurements (Jul 12, mid-attempt-4)

- 2-token completion round-trip: **13.0 s** (queueing behind agent work; single slot)
- GPU: Quadro RTX 6000, **100% util**, 23.2 / 24.6 GB VRAM
- llama-server: single slot, `-c 128000`, `-t 1`, no flash attention
- idle `ollama` snap co-resident on the box (VRAM risk)

### Docker cleanup (Jul 11, one-off sweep with the new reaper)

- 110 stale image tags removed (151 → 41)
- ~2.3 GB unique image layers + 6.37 GB build cache freed (~9 GB total; the
  40+ "3.8 GB" snapshots were mostly shared layers)
- Steady-state live working set after cleanup: ~26 GB

### Code delivered during the campaign

4 commits on `fix/verify-pipeline-e2e` (below); test suite grew **151 → 171**
(+20 tests), all passing.

## Bugs found & fixed during the campaign (all committed on fix/verify-pipeline-e2e)

- `e75ef99` — preflight now **waits** for the SSH tunnel instead of raising and
  killing a multi-hour run.
- `78db76c` — three gaps exposed by bug 439237851's first "solve" (a corrupt,
  harness-only patch recorded as verified_correct):
  - harness guard: any diff touching `oss-fuzz/` dirs or `*fuzz*` files is
    `patch_touches_harness`; neither the PoC re-run, `rake test`, nor the
    differential oracle can catch a harness rewrite, so the path check is the
    only gate;
  - `learn_loop._default_verify` now runs real verification (`verify_fix.verify`:
    fresh -vul container, rebuild, PoC re-run, `rake test`) instead of blessing
    any non-empty diff;
  - ledger keeps `grade()`'s error string (439237851's `oracle_error` turned out
    to be "corrupt patch at line 45" — the agent hand-wrote a broken diff after
    its `git diff` failed).
- `d58f28d` — patches counted only from the current run's summary; a no-patch run
  used to resurrect stale `oss_crs_patch_0.diff` files from old runs and get
  judged (and lectured) on them.
- `5e7777d` — stale per-run Docker images (snapshot test/build pairs, compose
  sets) reaped after every run; `content-*` incremental-build caches kept.
  One-off sweep freed ~9 GB (151 → 41 image tags).

## Why the campaign was killed

**Model serving is too slow for agentic loops at long context.** The agent's
conversation grows past ~100k tokens; llama-server runs a single slot, so the
Claude Code CLI's interleaved background calls (haiku/sonnet aliases route to the
same backend) evict the prefix KV cache, forcing full reprocessing of the whole
prompt on many turns. Measured: 13 s for a 2-token completion (queueing); agent
pace decayed from ~14 min/turn to ~80 min/turn as context grew. Attempt 4 ran
36 h, 102 turns, 5 edits without ever submitting, and drifted from root-cause
work into "very defensive fix" flailing — it cannot build/test in its container
(no make/rake toolchain), so it second-guesses endlessly.

Current llama-server invocation (from `ps` on the cloud box):

```
./llama-server -m .../Qwen3-Coder-30B-A3B-Instruct-IQ4_NL.gguf \
  --host 127.0.0.1 --port 8080 -c 128000 --no-mmap -ngl -1 -t 1
```

GPU: Quadro RTX 6000 24 GB, pegged at 100%, 23.1 GB used. An idle **ollama snap
also runs on the box** — a VRAM landmine if anything loads a model through it.

**The SSH tunnel resets every few hours** (7 drops Jul 10–13, all
`Read from remote host: Connection reset by peer` — server side). Restarts were
handled automatically this campaign (Claude Code babysits the tunnel as a
background task and relaunches on exit). Tunnel command with faster death
detection:

```
ssh -i ~/.ssh/cloud -N -o BatchMode=yes -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=4 -o ConnectTimeout=10 \
  -L 8080:localhost:8080 -L 172.17.0.1:8080:localhost:8080 cc@192.5.86.157
```

## Next steps (agreed / pending)

1. **Tune llama-server** (deferred to next session, over ssh): add `-fa` (flash
   attention), `--cache-reuse 256`, more than `-t 1` threads; consider
   `--parallel 2` trade-offs; stop the idle ollama snap. Re-measure turn latency
   at long context before relaunching a campaign.
2. **Investigate the periodic connection resets** on the cloud box (sshd logs,
   dmesg) — plausibly related to whatever else is unstable there.
3. **Add a wall-clock cap per CRS run** (`OSS_CRS_RUN_TIMEOUT`; timed-out run =
   no-patch attempt that gets feedback) so one attempt can never eat 36 h again.
   **Done** — set `OSS_CRS_RUN_TIMEOUT=<seconds>` (unset = no cap). On a timeout the
   agent subprocess is killed, the orphaned `crs_compose_*` containers are torn down
   (`terminate_crs_run`, so they stop pegging the GPU), and the run is recorded as a
   no-patch `timed_out` attempt; the repair loop feeds "you ran out of time, commit to
   a fix" forward to the next attempt. Next campaign should set e.g. `7200`.
4. Consider `LEARN_MAX_ATTEMPTS=3` for the next campaign.
5. Strategic option: one campaign on the OAuth/Claude compose
   (`OSS_CRS_COMPOSE_FILE=$HOME/oss-crs/example/crs-claude-code/compose-oauth.yaml`)
   to validate the full learning loop in hours, keeping the local model for
   after the serving fixes.
6. Bigger lift, known gap: the agent container has no build toolchain, so it
   can't self-validate patches (three of its tool errors every run are
   make/rake "command not found"). The outer verify loop compensates, but
   in-run validation would cut the flailing dramatically.

## State at kill time

- Ledger (`arvo-eval/results/learn/ledger.jsonl`): empty — no bug completed;
  next launch starts bug 439237851 from scratch under all the new fixes.
- Playbook store: empty (no lessons yet).
- `arvo-eval/results/treatment/439237851/`: attempt 3's rejected harness patch
  and its `patch_touches_harness` verification.json.
- All fixes above are committed; nothing uncommitted.
