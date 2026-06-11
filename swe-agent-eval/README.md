# SWE-Agent ARVO Repair Eval


## Setup

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

## Data

Currently using the existing ARVO database (`arvo.db`) for pipeline development and
validation. The final evaluation will use the newer dataset being rebuilt once it's ready.

`bug_ids.txt` contains 10 straightforward bugs from `arvo.db` (spanning curl, skia, mupdf,
imagemagick, harfbuzz, libxml2, wget2, and ffmpeg) used as a proof-of-concept validation set.

## Smoke test

First, confirm `arvo.db` is in place and all bugs load correctly (no API key needed):
```bash
python build_instance.py
```
You should see a one-line summary for each of the 10 bugs in `bug_ids.txt`.

Then confirm your model/API setup works with the built-in hello-world example:
```bash
python -m minisweagent.run.hello_world -m gemini/gemini-2.5-flash --task "Create a file called test.txt with the text 'it works' inside it"
```

## Running the eval

`run_single.py` runs mini-SWE-agent end-to-end on one ARVO bug: it pulls the bug's
Docker image, lets the agent attempt a fix, and saves the full trajectory under
`results/<bug_id>/trajectory.json`.

```bash
python run_single.py
```

By default it runs the bug ID hardcoded as `BUG_ID` in `run_single.py` using
`gemini/gemini-2.5-flash`. Override the model with the `MSWEA_MODEL_NAME` env var:

```bash
MSWEA_MODEL_NAME=gemini/gemini-2.5-pro python run_single.py
```
