"""The gallery's Archive, Restore and Delete (scratch.act): a name is one folder of
scratch and nothing else, archived folders leave the gallery but keep their links, and
Delete only ever empties the archive."""

import os

import pytest

from openbus import scratch


def _folder(root, name):
    (root / name).mkdir(parents=True)
    (root / name / "index.html").write_text(f"<title>{name}</title>")


@pytest.mark.parametrize("name", ["..", ".", "", "a/b", "../x", "/etc", ".archive", ".git",
                                  "a\0b"])
def test_a_name_is_one_folder_of_scratch(tmp_path, name):
    _folder(tmp_path, "a/b")
    with pytest.raises(ValueError, match="one folder"):
        scratch.act(tmp_path, "archive", ["ok", name])
    assert (tmp_path / "a" / "b").is_dir()


def test_a_link_is_never_followed(tmp_path):
    outside = tmp_path / "outside"
    _folder(outside, "victim")
    root = tmp_path / "scratch"
    root.mkdir()
    (root / "link").symlink_to(outside / "victim")
    assert scratch.act(root, "archive", ["link"]) == []
    (root / ".archive").rmdir()
    (root / ".archive").symlink_to(outside)  # a planted archive: refused outright
    with pytest.raises(PermissionError):
        scratch.act(root, "delete", ["victim"])
    assert (outside / "victim" / "index.html").is_file()


@pytest.mark.parametrize("renameat2", [True, False])
def test_archive_restore_and_delete(tmp_path, monkeypatch, renameat2):
    if not renameat2:  # a libc without it: the name is checked just before the move
        monkeypatch.setattr(scratch, "_RENAMEAT2", None)
    for name in ("old", "keep", "twin"):
        _folder(tmp_path, name)
    assert scratch.act(tmp_path, "archive", ["old", "twin", "old", "gone"]) == ["old", "twin"]
    assert [i["name"] for i in scratch.items(tmp_path)] == ["keep"]  # the archive is unlisted
    assert {i["name"] for i in scratch.listing(tmp_path, archived=True)} == {"old", "twin"}
    _folder(tmp_path, "twin")  # its name taken again: restoring it would clobber that one
    assert scratch.act(tmp_path, "unarchive", ["old", "twin"]) == ["old"]
    assert (tmp_path / "old" / "index.html").is_file()
    assert scratch.act(tmp_path, "delete", ["keep"]) == []  # never straight from the gallery
    assert scratch.act(tmp_path, "delete", ["twin"]) == ["twin"]
    assert not os.path.lexists(tmp_path / ".archive" / "twin")
    assert (tmp_path / "twin" / "index.html").is_file()


def test_the_archive_is_its_own_gallery(tmp_path):
    from openbus.server import WEB_DIR
    _folder(tmp_path, "old")
    style = WEB_DIR / "m" / "style.css"
    assert scratch.listing(tmp_path, archived=True) == []  # no archive yet
    scratch.act(tmp_path, "archive", ["old"])
    page = scratch.page(tmp_path, [], [], style, archived=True)
    assert page.startswith('<!doctype html><html class="archived">') and 'data-name="old"' in page
    assert 'href=".archive/old/"' in page  # its own link, even once "old" is taken again
    assert 'data-name="old"' not in scratch.page(tmp_path, [], [], style)
