"""Log levels reach journald as priorities: under systemd each stderr line carries its
sd-daemon "<N>" prefix so `journalctl -p warning` sees WARNING/ERROR, and only when
JOURNAL_STREAM names our actual stderr (an inherited value must not prefix a terminal)."""

import logging
import os

import pytest

from openbus import server


def _fmt(level, msg="hi"):
    rec = logging.LogRecord("t", level, __file__, 1, msg, None, None)
    return server.JournalFormatter("%(levelname)s %(message)s").format(rec)


@pytest.mark.parametrize(
    ("level", "prio"),
    [(logging.DEBUG, 7), (logging.INFO, 6), (logging.WARNING, 4), (logging.ERROR, 3),
     (logging.CRITICAL, 2), (25, 6)],
)
def test_level_maps_to_syslog_priority(level, prio):
    assert _fmt(level).startswith(f"<{prio}>")


def test_every_line_of_a_multiline_record_is_prefixed():
    assert _fmt(logging.ERROR, "a\nb").split("\n") == ["<3>ERROR a", "<3>b"]


def test_gated_on_journal_stream_matching_stderr(monkeypatch):
    st = os.fstat(2)
    monkeypatch.setenv("JOURNAL_STREAM", f"{st.st_dev}:{st.st_ino}")
    assert server.stderr_is_journal()
    monkeypatch.setenv("JOURNAL_STREAM", f"{st.st_dev}:{st.st_ino + 1}")  # inherited
    assert not server.stderr_is_journal()
    monkeypatch.delenv("JOURNAL_STREAM")
    assert not server.stderr_is_journal()
