"""Generic live status panel for long-running experiment pipelines.

Renders phase progress, arbitrary stats, and per-arm tallies as a `rich.Live`
panel, with a raw-passthrough buffer toggled by pressing 'v'. Nothing in here
knows about ARVO, mruby, or OSS-CRS -- callers supply phase labels, stat
key/values, and tally arms, so the same panel can back a different pipeline
later without changes.

Run this file directly for a simulated demo:
    .venv/bin/python3 live_status.py
"""
from __future__ import annotations

import select
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

try:
    import termios
    import tty
    _HAS_TERMIOS = True
except ImportError:
    _HAS_TERMIOS = False


class PhaseStatus(Enum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Phase:
    label: str
    status: PhaseStatus = PhaseStatus.PENDING
    elapsed: str | None = None


@dataclass
class Tally:
    label: str
    done: int
    total: int


SPINNER_FRAMES = ["✵", "✶", "✷", "✹", "✺", "✹", "✷", "✶"]


class LiveStatus:
    """A generic, pipeline-agnostic live status panel.

    Usage:
        status = LiveStatus(
            command="learn_loop.py --bugs 448044860",
            subject="bug 448044860 · control · attempt 1/5",
            position=(18, 30),
        )
        with status:
            status.set_phases([Phase("prepare environment", PhaseStatus.DONE, "0:04"), ...])
            status.set_stats({"tool calls": "37"})       # omit playbook key entirely on control
            status.set_tallies([Tally("control", 15, 15), Tally("treatment", 15, 15)])
            status.feed_raw(some_subprocess_stdout_line)
    """

    def __init__(
        self,
        command: str,
        subject: str,
        position: tuple[int, int] | None = None,
        console: Console | None = None,
        raw_maxlen: int = 500,
        on_abort: Callable[[], None] | None = None,
    ):
        self.command = command
        self.subject = subject
        self.position = position
        self.console = console or Console()
        self.on_abort = on_abort
        self._phases: list[Phase] = []
        self._stats: dict[str, str] = {}
        self._tallies: list[Tally] = []
        self._raw: deque[str] = deque(maxlen=raw_maxlen)
        self._show_raw = False
        self._spin_idx = 0
        self._abort_requested = False
        self._lock = threading.Lock()
        self._live: Live | None = None
        self._stop_keys = threading.Event()
        self._key_thread: threading.Thread | None = None

    def set_phases(self, phases: list[Phase]) -> None:
        with self._lock:
            self._phases = phases
        self._refresh()

    def set_stats(self, stats: dict[str, str]) -> None:
        with self._lock:
            self._stats = stats
        self._refresh()

    def set_tallies(self, tallies: list[Tally]) -> None:
        with self._lock:
            self._tallies = tallies
        self._refresh()

    def feed_raw(self, line: str) -> None:
        with self._lock:
            self._raw.append(line.rstrip("\n"))
        if self._show_raw:
            self._refresh()

    def __enter__(self) -> "LiveStatus":
        self._live = Live(self._render(), console=self.console, refresh_per_second=8, transient=False)
        self._live.__enter__()
        if _HAS_TERMIOS and sys.stdin.isatty():
            self._key_thread = threading.Thread(target=self._listen_keys, daemon=True)
            self._key_thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop_keys.set()
        if self._key_thread is not None:
            # Joined so the terminal is guaranteed restored (echo, canonical mode)
            # before this returns -- without it, callers printing right after the
            # `with` block would print into a still-raw terminal.
            self._key_thread.join(timeout=1.0)
        if self._live is not None:
            self._live.__exit__(*exc)

    @property
    def abort_requested(self) -> bool:
        with self._lock:
            return self._abort_requested

    def _handle_key(self, ch: str) -> None:
        if ch == "v":
            with self._lock:
                self._show_raw = not self._show_raw
            self._refresh()
        elif ch == "q":
            with self._lock:
                already = self._abort_requested
                self._abort_requested = True
            self._refresh()
            # Only fire once -- the callback tears down docker, which is not
            # idempotent-cheap to call repeatedly on repeated keypresses.
            if not already and self.on_abort is not None:
                self.on_abort()

    def _listen_keys(self) -> None:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            # A blocking sys.stdin.read(1) would never notice _stop_keys being set
            # until the NEXT keypress arrives, so the terminal-restoring finally
            # block below could be skipped entirely on exit (leaving the terminal
            # stuck without echo). Poll with a short timeout instead so the loop
            # re-checks _stop_keys on its own every 0.2s even with no input.
            while not self._stop_keys.is_set():
                ready, _, _ = select.select([fd], [], [], 0.2)
                if ready:
                    self._handle_key(sys.stdin.read(1))
        except Exception:
            pass
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render())

    def _next_spinner_frame(self) -> str:
        self._spin_idx = (self._spin_idx + 1) % len(SPINNER_FRAMES)
        return SPINNER_FRAMES[self._spin_idx]

    def _render(self) -> Group:
        with self._lock:
            phases = list(self._phases)
            stats = dict(self._stats)
            tallies = list(self._tallies)
            raw = list(self._raw)
            show_raw = self._show_raw
            aborting = self._abort_requested

        rows = []
        for p in phases:
            if p.status is PhaseStatus.DONE:
                mark, mark_style, label_style = "✓", "green", "grey70"
            elif p.status is PhaseStatus.ACTIVE:
                mark, mark_style, label_style = self._next_spinner_frame(), "yellow", "white"
            elif p.status is PhaseStatus.FAILED:
                mark, mark_style, label_style = "✗", "red", "white"
            else:
                mark, mark_style, label_style = "·", "grey50", "grey50"

            row = Text()
            row.append(f"  {mark}  ", style=mark_style)
            row.append(f"{p.label:<45}", style=label_style)
            if p.elapsed:
                row.append(p.elapsed, style="grey58")
            rows.append(row)

        subtitle = f"position {self.position[0]}/{self.position[1]}" if self.position else None
        panel = Panel(Group(*rows), title=self.subject, subtitle=subtitle, border_style="grey42")

        items = [Text(f"❯ {self.command}", style="grey58"), panel]

        stat_line = "    ".join(f"{k}: {v}" for k, v in stats.items())
        if stat_line:
            items.append(Text(stat_line, style="grey58"))

        tally_line = "    ".join(f"{t.label} {t.done}/{t.total} verified" for t in tallies)
        if tally_line:
            items.append(Text(tally_line, style="grey50"))

        if aborting:
            items.append(Text("aborting -- tearing down containers...", style="red"))
        controls = ("hide raw (v)" if show_raw else "show raw (v)") + "    abort (q)"
        items.append(Text(controls, style="grey35", justify="right"))

        if show_raw:
            raw_body = "\n".join(raw[-20:]) if raw else "(no output yet)"
            items.append(Panel(Text(raw_body, style="grey42"), border_style="grey23", height=12))

        return Group(*items)


def _demo() -> None:
    """Simulated run so the panel can be seen without wiring up a real pipeline."""
    status = LiveStatus(
        command="ARVO_DB_PATH=arvo_new.db LEARN_PASS=control .venv/bin/python3 learn_loop.py --bugs 448044860",
        subject="bug 448044860 · control · attempt 1/5",
        position=(18, 30),
    )
    plan = [
        ("prepare environment", 0.4, "0:04"),
        ("build target", 1.2, "4:52"),
        ("running agent · claude-opus-4-8", 2.5, None),
        ("verify fix · rebuild + PoC + rake test", 1.5, None),
        ("differential oracle · 6 probes + PoC", 1.0, None),
    ]
    raw_sample = [
        "crs-claude-code_patcher-1  INFO Agent setup complete",
        "crs-claude-code_patcher-1  INFO Found 1 POV(s): ['poc']",
        "crs-claude-code_patcher-1  INFO tool_use: Bash -- rake test 2>&1 | tail -80",
        "crs-claude-code_patcher-1  INFO tool_use: Edit -- src/numeric.c",
        "crs-claude-code_patcher-1  WARN test regression detected in mpz_mod, reverting",
    ]

    with status:
        phases = [Phase(label) for label, _, _ in plan]
        status.set_phases(phases)
        status.set_tallies([Tally("control", 15, 15), Tally("treatment", 15, 15)])

        for i, (label, dur, elapsed) in enumerate(plan):
            phases[i].status = PhaseStatus.ACTIVE
            status.set_phases(phases)
            steps = int(dur / 0.1)
            for s in range(steps):
                if raw_sample and s % 5 == 0:
                    status.feed_raw(raw_sample[(i + s) % len(raw_sample)])
                if label.startswith("running agent"):
                    status.set_stats({"tool calls": str(37 + s)})
                time.sleep(0.1)
            phases[i].status = PhaseStatus.DONE
            phases[i].elapsed = elapsed or f"{dur:.1f}s"
            status.set_phases(phases)
        time.sleep(1.5)


if __name__ == "__main__":
    _demo()
