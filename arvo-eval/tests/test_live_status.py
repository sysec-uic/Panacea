from rich.console import Console

from live_status import LiveStatus, Phase, PhaseStatus, Tally


def _status(**kw):
    # A Console writing to a null file avoids any real terminal dependency.
    console = Console(file=open("/dev/null", "w"), force_terminal=False)
    return LiveStatus(command="cmd", subject="subject", console=console, **kw)


def test_q_sets_abort_requested_and_fires_callback_once():
    calls = []
    status = _status(on_abort=lambda: calls.append(1))
    assert status.abort_requested is False

    status._handle_key("q")
    assert status.abort_requested is True
    assert calls == [1]

    status._handle_key("q")   # second press must not re-fire the teardown callback
    assert calls == [1]


def test_q_with_no_callback_does_not_raise():
    status = _status()
    status._handle_key("q")   # on_abort is None -- must be a no-op, not an error
    assert status.abort_requested is True


def test_v_toggles_show_raw():
    status = _status()
    assert status._show_raw is False
    status._handle_key("v")
    assert status._show_raw is True
    status._handle_key("v")
    assert status._show_raw is False


def test_other_keys_are_ignored():
    status = _status()
    status._handle_key("x")
    assert status.abort_requested is False
    assert status._show_raw is False


def _render_text(status: LiveStatus) -> str:
    console = Console(record=True, width=120, file=open("/dev/null", "w"), force_terminal=False)
    console.print(status._render())
    return console.export_text()


def test_stats_line_omitted_when_empty():
    status = _status()
    status.set_phases([Phase("build", PhaseStatus.DONE, "1:00")])
    status.set_stats({})
    assert "playbook" not in _render_text(status)


def test_render_includes_playbook_stat_only_when_provided():
    status = _status()
    status.set_stats({"playbook": "v7 heuristics"})
    assert "playbook: v7 heuristics" in _render_text(status)


def test_tallies_render_control_and_treatment():
    status = _status()
    status.set_tallies([Tally("control", 15, 15), Tally("treatment", 14, 15)])
    text = _render_text(status)
    assert "control 15/15 verified" in text
    assert "treatment 14/15 verified" in text


def test_abort_banner_shown_after_q():
    status = _status()
    assert "aborting" not in _render_text(status)
    status._handle_key("q")
    assert "aborting" in _render_text(status)
