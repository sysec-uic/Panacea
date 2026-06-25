# SWE-Agent ARVO Repair Eval

## Data

Currently using the existing ARVO database (`arvo.db`) for pipeline development and
validation. The final evaluation will use the newer dataset being rebuilt once it's ready.

`bug_ids.txt` contains 10 straightforward bugs from `arvo.db` (spanning curl, skia, mupdf,
imagemagick, harfbuzz, libxml2, wget2, and ffmpeg) used as a proof-of-concept validation set.

---

# Mini-SWE-Agent

### Setup

1. Create and activate a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Configure your model and API key (this writes to mini-SWE-agent's global config,
   not this repo):
   ```bash
   mini-extra config setup
   ```
   You'll need an API key for whichever model you choose (e.g. `GEMINI_API_KEY` for
   Gemini models, Google AI Studio offers a free tier).

### Smoke test

First, confirm `arvo.db` is in place and all bugs load correctly (no API key needed):
```bash
python3 build_instance.py
```
You should see a one-line summary for each of the 10 bugs in `bug_ids.txt`.

Then confirm your model/API setup works with the built-in hello-world example:
```bash
python3 -m minisweagent.run.hello_world -m gemini/gemini-2.5-flash --task "Create a file called test.txt with the text 'it works' inside it"
```

### Running

`run_single.py` runs mini-SWE-agent end-to-end on one ARVO bug: it pulls the bug's
Docker image, lets the agent attempt a fix, and saves the full trajectory under
`results/<bug_id>/trajectory.json`.

```bash
python3 run_single.py
```

By default it runs the bug ID hardcoded as `BUG_ID` in `run_single.py` using
`gemini/gemini-2.5-flash`. Override the model with the `MSWEA_MODEL_NAME` env var:

```bash
MSWEA_MODEL_NAME=gemini/gemini-2.5-pro python3 run_single.py
```

---

# OSS-CRS Pipeline (crs-claude-code)

Uses [OSS-CRS](https://github.com/ossf/oss-crs) with Claude Code as the patching agent. Won DARPA AIxCC. Generally more effective than mini-SWE-agent for C/C++ bugs due to better tooling and an incremental build loop.

### Prerequisites

- Clone OSS-CRS: `git clone https://github.com/ossf/oss-crs ~/oss-crs`
- Run `uv run oss-crs prepare --compose-file ~/oss-crs/example/crs-claude-code/compose-oauth.yaml` once
- Set `CLAUDE_CODE_OAUTH_TOKEN` in your shell (Claude Pro/Max OAuth token)
- **Ubuntu 20.04+ ARVO images only** — older images (e.g. wget2, bug 42470179) use Ubuntu 16.04 and are incompatible with OSS-CRS's install step

### Running

```bash
OSS_CRS_BUG_ID=435781342 python3 arvo_oss_crs.py
```

On subsequent runs, skip the Docker build step (reuses cached snapshot):

```bash
OSS_CRS_BUG_ID=435781342 python3 arvo_oss_crs.py --skip-build
```

If using a different database (e.g. `arvo_new.db`, available at https://github.com/sysec-uic/Panacea/releases/tag/ARVO_New_in_prog):
```bash
ARVO_DB_PATH=arvo_new.db OSS_CRS_BUG_ID=439279102 python3 arvo_oss_crs.py
```

Results are saved to `results/<bug_id>/oss_crs_result.json`. Patches are copied to `results/<bug_id>/oss_crs_patch_N.diff`. The agent's stdout log is saved to `results/<bug_id>/oss_crs_claude_stdout.log`.

### How it works

ARVO images don't match OSS-Fuzz's expected project format, so `arvo_oss_crs.py` generates a fake OSS-Fuzz project directory wrapping the ARVO Docker image (no-op `build.sh` since binaries are pre-compiled), extracts the POC from `/tmp/poc`, and drives OSS-CRS build + agent run. OSS-CRS handles the incremental build loop internally — after the first build it snapshots the container so patch attempts are fast.

### Token counts

Claude Code saves its session as a JSONL file inside the run's `LOG_DIR`. To extract token
usage after a run (file is root-owned, hence `sudo`):

```bash
SESSION=$(find ~/oss-crs/.oss-crs-workdir -name "*.jsonl" -path "*/.claude/*" | xargs ls -t 2>/dev/null | head -1)
sudo cat "$SESSION" | python3 -c "
import json, sys
inp = out = cache_r = cache_w = 0
for line in sys.stdin:
    u = json.loads(line).get('message', {}).get('usage', {})
    if u:
        inp += u.get('input_tokens', 0); out += u.get('output_tokens', 0)
        cache_r += u.get('cache_read_input_tokens', 0); cache_w += u.get('cache_creation_input_tokens', 0)
print(f'Input: {inp:,}  Output: {out:,}  Cache-read: {cache_r:,}  Cache-write: {cache_w:,}')
"
```

### Cost

Each run uses Claude Opus 4.8. Typical token usage for a successful patch (~300–700 s):

| Metric | Typical range |
|--------|--------------|
| Input tokens | ~8K–11K |
| Output tokens | ~30K–70K |
| Cache-read tokens | ~2M–4M |
| Cache-write tokens | ~100K–150K |

Cache-read dominates cost as the conversation grows across turns. Use `--skip-build` on reruns to avoid rebuilding the Docker snapshot.
