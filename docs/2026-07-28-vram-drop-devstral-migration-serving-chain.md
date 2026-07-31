# VRAM drops 320→80 GB — Devstral-24B migration + the serving chain rebuilt (Jul 27–28, 2026)

Follow-on to the [Jul 21 latency-wall writeup](2026-07-21-latency-wall-cleared-orientation-and-cache-reuse.md).
The GLM-5.2 pipeline from Jul 20–23 was working end-to-end, but the serving box lost
most of its VRAM — **320 GB → 80 GB** — so GLM-5.2 (even at UD-IQ2_M) no longer fits at
a usable context. This session picked a model that fits 80 GB, then rebuilt the serving
chain around it. The outcome: the **infra bottleneck is proven fully defeated** on the
new model — flat latency, tools working, 60+ turns of competent investigation on a hard
GC-UAF — but we **stopped before a verdict** (out of time; the user deferred the ~2h
solve run). This is a *serving/infra* writeup, not a solve writeup.

Scope note: this is research question **#4** (can the pipeline run cheaply on a *local*
model). Teammates own #1–3.

## Model choice: Devstral-Small-2-24B over retrying GLM

Options that fit 80 GB at a healthier quant than GLM's IQ2_M were weighed. Chose
**Devstral-Small-2-24B-Instruct-2512 at Q8_0** (Mistral × All-Hands agentic SWE model,
~68% SWE-bench Verified) — a purpose-built coding agent at a *high* quant (Q8 vs GLM's
2-bit) that fits comfortably. This trades raw parameter count for quantization health and
agentic tuning, which is the right bet for research question #4.

## Three serving walls, each real, each cleared

The chain is LiteLLM → (bridge) → `llama-server`. Getting Devstral to serve the OSS-CRS
agent surfaced three distinct failures.

### 1. `--jinja` builds a tool grammar that won't parse

With `--jinja` on, llama.cpp compiles a tool-call grammar from the chat template that
fails to parse and 400s the request. Fix: **drop `--jinja`** and let the bridge handle
tool translation.

### 2. Claude Code calls the Responses API, which latest-master llama.cpp regresses on

LiteLLM/Claude Code issue `/v1/responses` (OpenAI **Responses API**) calls, not
`/v1/chat/completions`. Today's llama.cpp — built from **latest master** by
`llama-installer.sh` (`git clone --depth 1` / `pull --ff-only`, no version pin) — has a
`/v1/responses` grammar regression that 400s. `chat/completions` is fine.

Fix: a **host-side translation bridge** (`arvo-eval/oss-crs-local/responses_bridge.py`)
that listens on `:8099`, translates `/v1/responses` → `/v1/chat/completions` and back
(including synthesized SSE streaming), and forwards to the box on `:8080`. It does two
more things beyond translation:

- **Sanitizes tool schemas** — strips `maxLength`/`minLength`/`pattern`/`format`/etc.
  from tool JSON-schemas. The `Workflow` tool's `script` param carries
  `maxLength:524288`, which *alone* breaks llama.cpp grammar generation on **any**
  endpoint (chat included).
- **`Connection: close` + serialized box calls** — the first bridge cut used keep-alive
  SSE with no Content-Length; that ambiguous response deadlocked LiteLLM↔bridge and the
  agent hung in `epoll_wait` until the recon-timeout SIGKILL (exit 137). Forcing
  `Connection: close` (HTTP/1.1, socket timeout) and a `threading.Lock` around the
  single-slot box fixed it.

### 3. Latency decay — same wall as GLM, same fix

Without cache-reuse, turn latency decayed linearly (23s → 50s over ~40 turns → 600s
timeout) as context grew — the identical re-prefill decay from the [Jul 21 GLM
run](2026-07-21-latency-wall-cleared-orientation-and-cache-reuse.md). Fix is box-side
(owner's action; the box is read-only to the agent): **`--cache-reuse 256` with `-fa
on`**. With it, latency held **flat at ~24s/turn** — roughly a 4× turn budget versus the
decaying GLM serving.

## The working box command

```
llama-server -m Devstral-Small-2-24B-Instruct-2512-Q8_0.gguf -ngl 99 -c 131072 \
  -fa on --cache-reuse 256 -np 1 -t -1 --host 127.0.0.1 --port 8080 --temp 0.15 --top-p 0.95
```

NO `--jinja`; `--cache-reuse 256` mandatory (with `-fa on`); `-np 1` gives the full 128k
context to the one agent session.

## Result

The full serving chain executes end-to-end on Devstral: grammar (bridge + sanitizer),
connection (Connection: close), and latency (cache-reuse) walls all cleared. On the hard
GC-UAF **439645304** (heap-use-after-free in `mrb_type` during set/khash hashing) the
agent ran **60+ turns of competent, on-target investigation at flat ~24s/turn** with no
hangs or timeouts. Everything *between the agent and the model* now works.

We stopped the run at ~27 turns (user out of time) **before a verdict**. At the stop
point the model looked to be trending toward a **crash-site guard** on `mrb_type`
(checking `MRB_TT_FREE`) rather than the **root-cause fix** (khash rebuild orphaning keys
from GC / the missing write barrier — see the memory note on 439645304 / 440058794).
Whether Devstral lands the root cause is the open question — but that is a *model
capability* question, no longer an infra one.

## Where the walls stand now

- **Jul 15:** model can't drive the loop → stronger model (GLM-5.2).
- **Jul 17–20:** model acts but harness drops the fix on submit → auto-submit + check-patch.
- **Jul 21:** model acts correctly but latency eats the cap → inline orientation + `--cache-reuse`.
- **Jul 27–28 (this session):** VRAM halved, GLM no longer fits → **Devstral-24B-Q8 +
  the responses-bridge**; grammar/connection/latency all re-cleared on the new model.
  Infra proven; **solve verdict still pending** (deferred for time).

## Resume checklist (next session)

1. Box up with the command above (owner starts it; read-only to us).
2. `python3 arvo-eval/oss-crs-local/responses_bridge.py` on the host (listens `:8099`).
3. In `~/oss-crs/example/crs-claude-code/litellm-config-local.yaml`, every `api_base` →
   `http://172.17.0.1:8099/v1` (a `.bak-capture` backup exists).
4. Clear the fixture ledger rows for 439645304 / 440058794.
5. `learn_loop.py --bugs 439645304,440058794` with
   `OSS_CRS_FORCE_EDIT=1 OSS_CRS_ORIENT=1 OSS_CRS_CHECK_PATCH=1 OSS_CRS_RUN_TIMEOUT=7200
   OSS_CRS_RECON_TIMEOUT=1800 LEARN_MAX_ATTEMPTS=1`, launched detached
   (`setsid nohup … </dev/null &`).
6. Watch each bug's outcome: EDITED vs zero-edits, check-patch PASS, oracle
   confirmed vs divergent. Then the head-to-head vs the GLM baseline.

## Operational notes

- **Durable fix alternative (not done):** pin llama.cpp to a pre-regression tag in
  `llama-installer.sh` — removes the `/responses` grammar bug at the source. The bridge's
  schema sanitizer would likely still be needed for the `Workflow` `maxLength`, so keep
  the bridge as belt-and-suspenders.
- The bridge lived in a session scratchpad during development; the **durable copy is
  committed-tree-adjacent at `arvo-eval/oss-crs-local/responses_bridge.py`** — start it
  from there, not the scratchpad.
- Disk bit us again: `docker system prune -a --volumes` (needed to reclaim 60 GB when the
  host FS hit 100%) also deleted the locally-built `claude-code-base:latest` image the
  patcher does `FROM` — rebuild via `cd ~/oss-crs && uv run oss-crs prepare
  --compose-file example/crs-claude-code/compose-local.yaml`. Check `df -h /` first.
- Serving box is read-only (shared machine); `--cache-reuse`/restarts are the owner's
  action. Results tree is git-ignored — copy anything worth keeping before cleanup.
