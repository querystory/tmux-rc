"""A line that addresses an agent in a Markdown file starts that agent, once
(docs/design/mention-trigger.md). The folder, notes and agent here are all invented."""

import asyncio
import os
import time

import pytest

from openbus import mentions, server, tmux
from tests.test_new_session import sock  # noqa: F401  (a fixture: a private tmux socket)

OLD = time.time() - 3600  # long past QUIET


def note(folder, name: str, text: str, changed: float = OLD):
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, (changed, changed))
    return path


@pytest.fixture
def watch(tmp_path):
    """A watched folder already read once (so what it then gains is new), and how to look
    at it again as a daemon just started would: (folder, due)."""
    folder = tmp_path / "notes"
    note(folder, "garden.md", "# Garden\n\n@claude which beds get morning sun\n\nThe east two.\n")

    def due(opened: int = 0) -> list[str]:
        fresh = mentions.Mentions(str(folder), tmp_path / "state" / "mentions.json", list)
        return [m for _, m in fresh.due(time.time(), opened)]

    assert due() == []  # enabling it on a folder that holds mentions starts nothing
    return folder, due


def test_reads_only_lines_that_address_an_agent():
    text = ("@Claude, plan the herb bed\n"
            "> @claude quoted from an old note\n"
            "    @claude indented as code\n"
            "ask @claude in passing\n"
            "```\n@codex inside a fence\n```\n"
            "@codex\n"
            "@rosemary is a plant, not an agent\n"
            "@codex: price the seeds\n")
    assert mentions.mentions(text) == ["claude plan the herb bed", "codex price the seeds"]


def test_a_mention_starts_once_and_not_again_after_a_restart(watch):
    folder, due = watch
    note(folder, "inbox/seeds.md", "@codex list what to sow in March\n")
    assert due() == ["codex list what to sow in March"]
    assert due() == []  # each call is a new daemon reading the same store


def test_the_answer_and_other_edits_start_nothing(watch):
    folder, due = watch
    path = note(folder, "seeds.md", "@codex list what to sow in March\n")
    assert len(due()) == 1
    note(folder, "seeds.md", "# Seeds\n\n@codex list what to sow in March\n\nPeas, beans.\n")
    path.rename(folder / "sowing.md")  # filed elsewhere: still the same mention
    assert due() == []
    note(folder, "sowing.md", "")  # emptied and written back, as a sync or a checkout does
    assert due() == []
    note(folder, "sowing.md", "@codex list what to sow in March\n")
    assert due() == []


def test_the_same_request_written_again_is_a_new_mention(watch):
    folder, due = watch
    note(folder, "today.md", "@claude which beds get morning sun\n")
    assert due() == ["claude which beds get morning sun"]


def test_a_file_still_being_written_waits(watch):
    folder, due = watch
    note(folder, "seeds.md", "@claude compare the two", changed=time.time())
    assert due() == []
    note(folder, "seeds.md", "@claude compare the two catalogues\n")
    assert due() == ["claude compare the two catalogues"]


def test_agents_writing_mentions_cannot_open_windows_without_bound(watch):
    """However many mentions agents write, OPEN windows is all that can be open; the rest
    wait for one to close rather than being dropped."""
    folder, due = watch
    note(folder, "handoff.md", "".join(f"@claude step {n}\n" for n in range(mentions.OPEN + 3)))
    assert due(opened=mentions.OPEN) == []
    assert len(due(opened=2)) == mentions.OPEN - 2
    assert due(opened=mentions.OPEN) == []
    assert len(due(opened=0)) == 5  # the 3 already started are not among them
    assert due(opened=0) == []


def test_nothing_outside_the_folder_is_read(watch, tmp_path):
    folder, due = watch
    outside = note(tmp_path / "elsewhere", "private.md", "@claude read me\n")
    (folder / "linked.md").symlink_to(outside)
    (folder / "linked").symlink_to(outside.parent, target_is_directory=True)
    note(folder, ".hidden/draft.md", "@claude not from a dot folder\n")
    note(folder, "plan.txt", "@claude not from a text file\n")
    assert due() == []


def test_a_folder_that_does_not_exist_yet_is_first_read_when_it_appears(tmp_path):
    watcher = mentions.Mentions(str(tmp_path / "later"), tmp_path / "mentions.json", list)
    assert watcher.due(time.time(), 0) == []
    note(tmp_path / "later", "old.md", "@claude from before it was watched\n")
    assert watcher.due(time.time(), 0) == []


@pytest.mark.parametrize(("env", "started"), [({}, 0), ({"TMUXRC_MENTIONS_DIR": "/notes"}, 1),
                                              ({"TMUXRC_MENTIONS_DIR": "/notes",
                                                "TMUXRC_TARGET": "%1"}, 0)])
def test_off_unless_configured(monkeypatch, env, started):
    made = []

    async def nothing(*_args):
        pass

    for name in ("TMUXRC_MENTIONS_DIR", "TMUXRC_TARGET"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(mentions.Mentions, "__init__", lambda _self, *args: made.append(args))
    monkeypatch.setattr(mentions.Mentions, "run", nothing)
    monkeypatch.setattr(server.Watcher, "start", lambda _self: None)
    monkeypatch.setattr(server.Watcher, "stop", nothing)
    monkeypatch.setattr(server, "_advertise_scratch", lambda: None)  # never the live tmux

    async def check():
        async with server.lifespan(server.app):
            assert len(made) == started

    asyncio.run(check())


def test_the_window_runs_the_configured_agent_on_the_request_unparsed(sock, tmp_path):  # noqa: F811
    """On a private tmux server, with a stand-in for the agent: it is started in the folder
    with the request as one argument, whatever shell syntax the request holds."""
    folder, got = tmp_path / "notes", tmp_path / "got"
    folder.mkdir()
    agent = tmp_path / "agent"
    agent.write_text(f'#!/bin/sh\nprintf "%s|%s|%s" "$#" "$PWD" "$2" > {got}.tmp\n'
                     f"mv {got}.tmp {got}\nexec sleep 30\n")
    agent.chmod(0o755)
    watcher = mentions.Mentions(str(folder), tmp_path / "mentions.json",
                                lambda: [{"icon": "claude", "command": f"{agent} --flag"}])
    assert watcher.poll() == []
    request = 'prune "$(touch pwned)"; `touch pwned` && touch pwned #{pane_id}'
    note(folder, "trees.md", f"@claude {request}\n")
    assert len(watcher.poll()) == 1
    for _ in range(100):
        if got.exists():
            break
        time.sleep(0.05)
    count, cwd, prompt = got.read_text().split("|", 2)
    assert (count, cwd) == ("2", str(folder))  # --flag and the prompt
    assert prompt == mentions.PROMPT.format(file="trees.md", request=request)
    assert not list(tmp_path.rglob("pwned"))
    assert [p.window_name for p in tmux.list_panes()] == ["@claude trees"]
    assert mentions.windows() == 1
    assert watcher.poll() == []
