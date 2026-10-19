import pytest

from openbus import classify, tmux


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
        cleared = tmux.capture_pane(pane, mark_dim=True)
        parts = cleared.split(tmux.VISIBLE_SCREEN)
        assert len(parts) in (1, 2)
        if len(parts) == 2:
            assert not parts[0].strip()
        assert "\n1\n" not in cleared
        assert "\n30\n" in parts[-1]
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
        assert not text.endswith("\n")  # the cursor is on the shell prompt's row
        run(["send-keys", "-t", pane, "echo done; sleep 9", "Enter"])
        for _ in range(50):
            text = tmux.capture_pane(pane, mark_dim=True)
            if text.endswith("\ndone\n"):
                break
            time.sleep(0.02)
        assert text.endswith("\ndone\n")  # one empty row: the cursor, below the output
    finally:
        subprocess.run(["tmux", "-S", socket, "kill-server"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_classifier_capture_splits_before_wrapping(monkeypatch):
    calls = []

    def run(args):
        calls.append(args)
        return f"old menu\n{args[9]}\n› \x1b[2mAsk a question\x1b[0m\n\n-2 80\n"

    monkeypatch.setattr(tmux, "_run", run)
    text = tmux.capture_pane("%12", mark_dim=True)
    assert len(calls[0][9]) == 32 and all(c in "0123456789abcdef" for c in calls[0][9])
    assert calls == [[
        "if-shell", "-F", "-t", "%12", "#{>:#{history_size},0}",
        "capture-pane -p -J -e -t %12 -S -200 -E -1",
        ";", "display-message", "-p", calls[0][9],
        ";", "capture-pane", "-p", "-J", "-e", "-t", "%12", "-S", "0",
        ";", "display-message", "-p", "-t", "%12",
        "#{e|-|:#{cursor_y},#{pane_height}} #{pane_width}",
    ]]
    assert text == f"old menu\n{tmux.VISIBLE_SCREEN}\n› ⟪placeholder⟫Ask a question⟪/placeholder⟫"
    assert tmux.strip_dim(text) == "old menu\n› Ask a question"


def test_phone_capture_does_not_add_boundary(monkeypatch):
    calls = []
    monkeypatch.setattr(tmux, "_run", lambda args: calls.append(args) or "plain\n")
    assert tmux.capture_pane("%1") == "plain"
    assert "if-shell" not in calls[0]


def test_bootstrap_capture_uses_its_history_budget(monkeypatch):
    calls = []
    monkeypatch.setattr(tmux, "_run", lambda args: calls.append(args) or args[9] + "\n-1 80\n")
    tmux.capture_pane("%1", lines=800, mark_dim=True)
    assert "-S -800 -E -1" in calls[0][5]


def test_printed_boundary_label_is_preserved():
    text = f"history\n{tmux.VISIBLE_SCREEN}\n[visible screen]\ncurrent output"
    assert tmux.strip_dim(text) == "history\n[visible screen]\ncurrent output"
    assert tmux.strip_dim(tmux.VISIBLE_SCREEN) == ""
    assert tmux.strip_dim("history\n" + tmux.VISIBLE_SCREEN) == "history\n"


@pytest.mark.parametrize(("screen", "cursor", "want"), [
    ("Ok? (y) \n\n", -2, "Ok? (y) "),  # the cursor on the prompt's row
    ("Ok? (y) \nworking\n\n", -1, "Ok? (y) \nworking\n"),  # on a blank row below output
    ("box\n\n\x1b[2mfooter\x1b[0m\n", -2, "box\n\n⟪dim⟫footer⟪/dim⟫"),  # above a footer
])
def test_classifier_capture_ends_on_a_blank_cursor_row(monkeypatch, screen, cursor, want):
    monkeypatch.setattr(tmux, "_run", lambda args: f"{args[9]}\n{screen}{cursor} 80\n")
    assert tmux.capture_pane("%1", mark_dim=True) == f"{tmux.VISIBLE_SCREEN}\n{want}"


def _tall_screen() -> list[str]:
    """A 500-row agent window: an answered question near the top, a long run of later
    output, the current idle prompt, then blank rows to the window's bottom."""
    old = ["Which color should the banner use?", "  1. Teal", "  2. Amber", "❯ 1",
           "Painting the banner teal."]
    work = [f"step {i}" for i in range(300)]
    return ["earlier turn", *old, *work, "Done: banner is teal.", "❯ ", *[""] * 188]


def test_tall_screen_keeps_only_its_bottom_rows_visible(monkeypatch):
    rows = _tall_screen()
    monkeypatch.setattr(tmux, "_run", lambda args: f"{args[9]}\n" + "\n".join(rows) + "\n-188 80\n")
    history, visible = tmux.capture_pane("%1", mark_dim=True).split(tmux.VISIBLE_SCREEN)
    assert visible.strip("\n").split("\n") == rows[-188 - tmux.VIEWPORT_ROWS:-188]
    assert visible.endswith("\n")  # the cursor row below the prompt still counts
    # Every row above is still sent, as history, whatever the history budget.
    assert history.rstrip("\n").split("\n") == rows[:-188 - tmux.VIEWPORT_ROWS]


def test_tall_screen_old_question_is_off_the_gated_screen(monkeypatch):
    rows = _tall_screen()
    monkeypatch.setattr(tmux, "_run", lambda args: f"{args[9]}\n" + "\n".join(rows) + "\n-188 80\n")
    text = tmux.capture_pane("%1", mark_dim=True)
    assert "Which color should the banner use?" in text  # the model still reads it, as history
    assert "Which color should the banner use?" not in classify._visible(text)
    assert classify.screen_offers(["Painting the banner teal."], text) == []
    assert classify.screen_offers(["Done: banner is teal."], text) == ["Done: banner is teal."]


def test_wrapped_output_counts_its_physical_rows(monkeypatch):
    # -J joins each 200-cell line from the three 80-cell rows it wrapped across.
    rows = ["Which color?", *[f"{i:03} " + "x" * 196 for i in range(100)], "❯ "]
    monkeypatch.setattr(tmux, "_run", lambda args: f"{args[9]}\n" + "\n".join(rows) + "\n-1 80\n")
    history, visible = tmux.capture_pane("%1", mark_dim=True).split(tmux.VISIBLE_SCREEN)
    assert "Which color?" in history
    assert visible.strip("\n").split("\n") == rows[-51:]  # 50 x 3 rows: the top straddles


def test_short_screen_is_unchanged(monkeypatch):
    out = "old\n{}\na\n\n" + "\n" * 300 + "-300 80\n"
    monkeypatch.setattr(tmux, "_run", lambda args: out.format(args[9]))
    assert tmux.capture_pane("%1", mark_dim=True) == f"old\n{tmux.VISIBLE_SCREEN}\na\n"


def test_real_tall_pane_moves_its_top_rows_into_history(tmp_path, monkeypatch):
    import shutil
    import subprocess
    import time

    if not shutil.which("tmux"):
        pytest.skip("tmux is not installed")
    socket = str(tmp_path / "tall.sock")

    def run(args):
        return subprocess.check_output(["tmux", "-S", socket, *args], text=True)

    try:
        pane = run(["new-session", "-d", "-P", "-F", "#{pane_id}",
                    "-x", "80", "-y", "400", "sh"]).strip()
        monkeypatch.setattr(tmux, "_run", run)
        run(["send-keys", "-t", pane, "seq 1 300", "Enter"])
        for _ in range(100):
            if "\n300\n" in tmux.capture_live(pane)[0]:
                break
            time.sleep(0.02)
        history, visible = tmux.capture_pane(pane, mark_dim=True).split(tmux.VISIBLE_SCREEN)
        assert "\n1\n" in history and "\n1\n" not in visible
        assert "\n300\n" in visible
        assert len(visible.strip("\n").split("\n")) == tmux.VIEWPORT_ROWS
        assert "\n1\n" in tmux.capture_live(pane)[0]  # the live view is whole
    finally:
        subprocess.run(["tmux", "-S", socket, "kill-server"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
