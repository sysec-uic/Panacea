# Control baseline, and a silent injection-delivery bug that was running treatment as control (Jul 23, 2026)

Follow-on to the [Jul 21 latency-wall writeup](2026-07-21-latency-wall-cleared-orientation-and-cache-reuse.md).
Same setup: the chronological control/treatment repair-and-learn loop over mruby ARVO
bugs, repair agent = OSS-CRS crs-claude-code driving a GLM-class local model over an
SSH tunnel, run detached under `systemd-inhibit`, with the in-turn `check-patch`
self-check, per-run wall-clock cap, and crash orientation all enabled.

This session set out to close the **control-pass gap** — and in doing so uncovered a
delivery bug that had been silently invalidating part of the treatment arm.

## Goal

Establish a matched **control** baseline on the local model. The research question is
whether injecting a playbook of lessons learned from earlier bugs (the *treatment* arm)
helps the agent fix later bugs, versus no memory (the *control* arm). Coming in, only
treatment had ever run on the local model, so there was no control baseline to measure
the playbook's effect against — "the signal is visible" but not yet *measured* on this
backend.

## What we did

### 1. Fixed a run-isolation hazard first

Control and treatment shared the same per-bug working directory and a single results
ledger, so the two arms could clobber each other's working tree and interleave ledger
writes — unsafe to run both and a latent hazard even sequentially. Added per-pass
namespacing: the agent's project tree and the ledger are now keyed by pass, and the two
independent computations of the project-dir path were collapsed into one shared helper
so they can never diverge. Committed with a regression test.

### 2. Ran the control pass — 6/10

Control ran over the first ten bugs in chronological order: **6/10 solved (60%)**. Every
failure was a *legitimate* run — the agent engaged for many turns or worked its full
wall-clock budget and still didn't converge — not an infrastructure artifact. Two of the
solves were bugs treatment had also solved earlier; four failures were genuinely hard
memory-safety bugs.

### 3. Cleared two infrastructure incidents live

- **Container-network address-pool exhaustion.** The container runtime accumulates a
  network per run and never reclaims them; after enough runs it ran out of address space,
  the agent's container network failed to create, and the run produced a *false*
  "agent-never-ran" result (zero work, looks like a no-op failure). Mitigated by pruning
  unused networks between runs; the proper fix is per-run teardown of each run's networks.
- **A run that looked stuck** turned out to be legitimately sitting at the wall-clock cap.
  Confirmed the timeout fires correctly and releases to the next bug — a false alarm, no
  intervention needed, and a good check that the cap mechanism works.

### 4. Switched to the head-to-head

At the ten-bug mark, stopped control and started treatment on the same bugs (with the
playbook), monitoring on a periodic loop. Two treatment failures on hard bugs prompted a
closer look — which surfaced the headline finding.

## Headline finding: treatment was silently running as control

Reading the agent transcripts revealed a **perfect correlation**: when the playbook — and
the "make your best edit early, then validate it with the in-loop check tool, and iterate"
guidance — actually reached the agent, it made an edit and solved the bug. When it did
*not* reach the agent, the agent stayed in read-only investigation, made **zero edits**,
reasoned its way right up to the fix, and then stopped without ever writing a patch — the
exact "understand everything, submit once" behavior an earlier, weaker model had shown.

Root cause: the routine that locates the agent's source directory (where the guidance must
be written) selected the **globally most-recently-modified** candidate among *many*
accumulated directories — the work area keeps one per build across every run and never
cleans them. Ordinary activity (concurrent builds, the in-loop check's incremental
rebuilds, a warm validation container) constantly bumps these timestamps, so the guidance
was intermittently written into the **wrong** run's directory. The agent then found no
guidance, ran without the playbook *and* without knowing the check tool existed, and that
"treatment" run **silently degraded into a control run**.

Consequence: the earlier read that "the playbook gave no lift on the discriminating bugs"
was **invalid** — on those runs the playbook was never delivered. The comparison had been
quietly contaminated.

## The fix

Pin the injection to *this* run's freshly-built source directory by **identity** rather
than by timestamp: snapshot the set of existing directories and the wall clock *before*
the build, then select the directory that newly appeared — immune to timestamp churn.
If no fresh directory is found, it now **fails loudly** (logging that the run is
effectively control) instead of guessing, and it verifies the write landed and logs the
exact destination and size, so a missed delivery is auditable from the run log rather than
silent. This mirrors an equivalent race guard already used elsewhere in the harness for
the check-tool channel. Committed with a regression test; the full suite passes.

Then **audited** every fresh treatment run — grep the transcript for the "no guidance
found" signature versus the guidance text — and **cleared** the two contaminated entries
from the ledger.

## Corrected standings

- **Control: 6/10** — valid and unaffected (control injects nothing, so the delivery bug
  can't touch it).
- **Treatment: valid only where delivery is confirmed** — the earlier solves plus one
  fresh solve that provably received the playbook. The contaminated bugs need a clean
  re-run for a true head-to-head.

## Lessons

- **"We built the guidance" ≠ "the agent received it."** The instruction to iterate
  quickly was correct and present in the code the whole time; a delivery bug kept it from
  reaching the agent on some runs. Verify that guidance actually lands per run, not just
  that the code emits it.
- The in-loop **validate-and-iterate guidance appears causally important for whether the
  agent commits to an edit at all** — its presence tracked one-to-one with success across
  these runs. That reframes it from a convenience into a load-bearing part of the loop.
- A pipeline can look like it's producing clean experimental data while silently
  mislabeling runs. **Transcript-level auditing caught what the ledger alone could not** —
  worth doing routinely, not just when something looks off.
- Unbounded accumulation in a shared work area (never-cleaned build directories) was the
  soil this bug grew in. "Most recently modified" is a fragile way to identify the current
  run's artifacts once that area is crowded and noisy.

## State

Everything is stopped and idle; nothing is mid-run. The isolation fix and the delivery fix
are both committed. A clean re-run of the affected bugs — with delivery now verified per
run — is the next step whenever the campaign resumes.
