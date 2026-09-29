from openbus import tmux


def test_real_tmux_history_boundary(tmp_path, monkeypatch):
    import shutil
    import subprocess
    import time

    import pytest

    if not shutil.which("tmux"):
        pytest.skip("tmux is not installed")
    socket = str(tmp_path / "capture.sock")

    def run(args):
        return subprocess.check_output(["tmux", "-S", socket, *args], text=True)

    try:
        pane = run(["new-session", "-d", "-P", "-F", "#{pane_id}",
                    "-x", "80", "-y", "10", "sh"]).strip()
        monkeypatch.setattr(tmux, "_run", run)
        run(["send-keys", "-t", pane, "seq 1 30", "Enter"])
        for _ in range(50):
            if int(run(["display-message", "-p", "-t", pane, "#{history_size}"])) > 1:
                break
            time.sleep(0.02)
        text = tmux.capture_pane(pane, mark_dim=True)
        history, visible = text.split(tmux.VISIBLE_SCREEN)
        assert "\n1\n" in history
        assert "\n30\n" in visible
        run(["clear-history", "-t", pane])
        empty_history, _ = tmux.capture_pane(pane, mark_dim=True).split(tmux.VISIBLE_SCREEN)
        assert not empty_history
        # Terminal controls cannot become stored cells even when the pane writes
        # the complete framed token. Only the capture nonce becomes a boundary.
        run(["send-keys", "-t", pane, r"printf '\036[visible screen]\037\n'", "Enter"])
        for _ in range(50):
            text = tmux.capture_pane(pane, mark_dim=True)
            if "\n[visible screen]\n" in text:
                break
            time.sleep(0.02)
        assert "\n[visible screen]\n" in text
        assert text.count(tmux.VISIBLE_SCREEN) == 1
    finally:
        subprocess.run(["tmux", "-S", socket, "kill-server"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_classifier_capture_splits_before_wrapping(monkeypatch):
    calls = []

    def run(args):
        calls.append(args)
        return f"old menu\n{args[9]}\n› \x1b[2mAsk a question\x1b[0m\n"

    monkeypatch.setattr(tmux, "_run", run)
    text = tmux.capture_pane("%12", mark_dim=True)
    assert len(calls[0][9]) == 32 and all(c in "0123456789abcdef" for c in calls[0][9])
    assert calls == [[
        "if-shell", "-F", "-t", "%12", "#{>:#{history_size},0}",
        "capture-pane -p -J -e -t %12 -S -200 -E -1",
        ";", "display-message", "-p", calls[0][9],
        ";", "capture-pane", "-p", "-J", "-e", "-t", "%12", "-S", "0",
    ]]
    assert text == f"old menu\n{tmux.VISIBLE_SCREEN}\n› ⟪placeholder⟫Ask a question⟪/placeholder⟫"
    assert tmux.strip_dim(text) == "old menu\n› Ask a question"


def test_phone_capture_does_not_add_boundary(monkeypatch):
    calls = []
    monkeypatch.setattr(tmux, "_run", lambda args: calls.append(args) or "plain\n")
    assert tmux.capture_pane("%1", keep_colors=True) == "plain"
    assert "if-shell" not in calls[0]


def test_bootstrap_capture_uses_its_history_budget(monkeypatch):
    calls = []
    monkeypatch.setattr(tmux, "_run", lambda args: calls.append(args) or args[9] + "\n")
    tmux.capture_pane("%1", lines=800, mark_dim=True)
    assert "-S -800 -E -1" in calls[0][5]


def test_printed_boundary_label_is_preserved():
    text = f"history\n{tmux.VISIBLE_SCREEN}\n[visible screen]\ncurrent output"
    assert tmux.strip_dim(text) == "history\n[visible screen]\ncurrent output"
    assert tmux.strip_dim(tmux.VISIBLE_SCREEN) == ""
    assert tmux.strip_dim("history\n" + tmux.VISIBLE_SCREEN) == "history\n"
