"""tmux.click turns a frame-relative tap into an SGR mouse report on the pane.

Rows count up from the frame's last line, so they must resolve against the VISIBLE
screen's last line, and nothing may be written to a pane whose app didn't ask for
mouse reports (a shell would echo it as garbage)."""
import openbus.tmux as T


def fake_tmux(monkeypatch, screen, sgr="1", physical=None):
    sent = []

    def run(argv):
        if argv[0] == "display-message":
            return f"80 {sgr}\n"
        if argv[0] == "capture-pane":
            assert argv[-1] == "-0"  # the visible screen only, not history
            return physical if "-J" not in argv and physical is not None else screen
        sent.append(argv)
        return ""

    monkeypatch.setattr(T, "_run", run)
    return sent


def report(argv):
    return bytes.fromhex("".join(argv[argv.index("-H") + 1:])).decode()


def test_row_counts_up_from_visible_last_line(monkeypatch):
    # 4 visible rows plus the trailing blanks the live frame also rstrips
    sent = fake_tmux(monkeypatch, "a\nb\nc\nd\n\n\n")
    assert T.click("%1", from_bottom=1, col=5)
    assert report(sent[0]) == "\x1b[<0;5;3M\x1b[<0;5;3m"


def test_no_mouse_mode_sends_nothing(monkeypatch):
    sent = fake_tmux(monkeypatch, "a\nb", sgr="0")
    assert not T.click("%1", from_bottom=0, col=1)
    assert sent == []


def test_tap_on_history_sends_nothing(monkeypatch):
    sent = fake_tmux(monkeypatch, "a\nb")
    assert not T.click("%1", from_bottom=2, col=1)
    assert sent == []


def test_wrapped_screen_is_not_safe_to_click(monkeypatch):
    sent = fake_tmux(monkeypatch, "long joined line\nlast", physical="long\njoined line\nlast")
    assert not T.click("%1", from_bottom=0, col=1)
    assert sent == []


def test_out_of_bounds_coordinates_send_nothing(monkeypatch):
    sent = fake_tmux(monkeypatch, "a\nb")
    assert not T.click("%1", from_bottom=-1, col=1)
    assert not T.click("%1", from_bottom=0, col=0)
    assert not T.click("%1", from_bottom=0, col=81)
    assert sent == []
