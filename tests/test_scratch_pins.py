"""Pins: scratch folders the app's menu keeps (scratch.pinned, scratch.pin), stored by the
daemon so every device shows the same ones."""

import json

import pytest

from openbus import scratch


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "scratch"
    for name, meta in (("flows", {"title": "Flows", "icon": "network"}), ("a", {}), ("b", {}),
                       ("c", {}), ("d", {}), ("e", {}), ("f", {})):
        (root / name).mkdir(parents=True)
        (root / name / "index.html").write_text("<title>Page</title>")
        (root / name / "meta.json").write_text(json.dumps(meta))
    return root


def test_pin_then_unpin_keeps_order_title_and_icon(root, tmp_path):
    store = tmp_path / "state" / "pins.json"
    assert scratch.pinned(root, store) == []  # nothing pinned yet: no file
    pins = scratch.pin(root, store, ["a", "flows"])
    assert [(p["name"], p["title"], p["icon"], p["href"]) for p in pins] == [
        ("a", "Page", None, "a/"), ("flows", "Flows", "network", "flows/")]
    assert scratch.pinned(root, store) == pins
    assert [p["name"] for p in scratch.pin(root, store, ["flows"])] == ["flows"]


def test_at_most_six_each_once(root, tmp_path):
    store = tmp_path / "pins.json"
    assert len(scratch.pin(root, store, ["a", "b", "c", "d", "e", "f"])) == 6
    for names in (["a", "b", "c", "d", "e", "f", "flows"], ["a", "a"]):
        with pytest.raises(ValueError, match="at most"):
            scratch.pin(root, store, names)
    assert len(scratch.pinned(root, store)) == 6  # a refused pin changes nothing


@pytest.mark.parametrize("name", ["..", ".", "", "a/../b", "../scratch/a", ".archive",
                                  "/etc", "a\0", "missing", "link"])
def test_only_a_folder_of_scratch_is_pinned(root, tmp_path, name):
    (root / "link").symlink_to(root / "a")
    store = tmp_path / "pins.json"
    with pytest.raises(ValueError, match="one folder"):
        scratch.pin(root, store, ["a", name])
    assert not store.exists()


def test_a_deleted_or_archived_folder_drops_out(root, tmp_path):
    store = tmp_path / "pins.json"
    scratch.pin(root, store, ["a", "b", "flows"])
    (root / ".archive").mkdir()
    (root / "a").rename(root / ".archive" / "a")  # the gallery's Archive moves it out
    for f in (root / "b").iterdir():
        f.unlink()
    (root / "b").rmdir()
    assert [p["name"] for p in scratch.pinned(root, store)] == ["flows"]
    store.write_text('["../x", 3, "flows"]')  # a hand-edited file: only real folders count
    assert [p["name"] for p in scratch.pinned(root, store)] == ["flows"]
    store.write_text("{}")
    assert scratch.pinned(root, store) == []
