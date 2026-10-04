from __future__ import annotations

import os
from pathlib import Path

import pytest

from organizer.organize_files import main


def make_file(path: Path, content: str = "content") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def run(source: Path, dest: Path, *extra: str) -> int:
    return main(["--source", str(source), "--dest", str(dest), *extra])


def test_dry_run_moves_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "inbox"
    dest = tmp_path / "organized"
    report = make_file(source / "report.pdf", "pdf")
    photo = make_file(source / "photo.jpg", "jpg")

    assert run(source, dest) == 0

    assert report.read_text() == "pdf"
    assert photo.read_text() == "jpg"
    assert not dest.exists()
    stderr = capsys.readouterr().err
    assert "WOULD MOVE" in stderr
    assert "Dry run complete" in stderr


def test_apply_groups_files_and_writes_audit_log(tmp_path: Path) -> None:
    source = tmp_path / "inbox"
    dest = tmp_path / "organized"
    files = {
        "report.pdf": "documents",
        "notes.md": "documents",
        "photo.JPG": "images",
        "table.csv": "data",
        "bundle.tar.gz": "archives",
        "README": "other",
    }
    for name in files:
        make_file(source / name, name)

    assert run(source, dest, "--apply") == 0

    for name, category in files.items():
        assert not (source / name).exists()
        moved = dest / category / name
        assert moved.is_file()
        assert moved.read_text() == name

    log = dest / "organize_files.log"
    assert log.is_file()
    text = log.read_text(encoding="utf-8")
    assert "MOVED" in text
    assert "Applied:" in text


def test_never_overwrites_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "inbox"
    dest = tmp_path / "organized"
    make_file(source / "report.pdf", "new")
    existing = make_file(dest / "documents" / "report.pdf", "old")

    assert run(source, dest, "--apply") == 0

    assert existing.read_text() == "old"
    assert not (source / "report.pdf").exists()
    numbered = dest / "documents" / "report_1.pdf"
    assert numbered.read_text() == "new"


def test_collision_with_numbered_name_increments(tmp_path: Path) -> None:
    source = tmp_path / "inbox"
    dest = tmp_path / "organized"
    make_file(source / "report.pdf", "new")
    make_file(dest / "documents" / "report.pdf", "existing")
    make_file(dest / "documents" / "report_1.pdf", "existing one")

    assert run(source, dest, "--apply") == 0
    assert (dest / "documents" / "report_2.pdf").read_text() == "new"


def test_broken_symlink_at_destination_counts_as_collision(tmp_path: Path) -> None:
    source = tmp_path / "inbox"
    dest = tmp_path / "organized"
    make_file(source / "report.pdf", "new")
    (dest / "documents").mkdir(parents=True)
    broken = dest / "documents" / "report.pdf"
    try:
        broken.symlink_to(tmp_path / "missing.pdf")
    except OSError:
        pytest.skip("symlinks are not supported in this environment")

    assert run(source, dest, "--apply") == 0

    assert os.path.lexists(broken)  # the broken symlink was not overwritten
    assert (dest / "documents" / "report_1.pdf").read_text() == "new"


def test_hidden_and_symlinks_are_skipped_by_default(tmp_path: Path) -> None:
    source = tmp_path / "inbox"
    dest = tmp_path / "organized"
    hidden = make_file(source / ".secret.pdf", "hidden")
    target = make_file(source / "target.pdf", "target")
    link = source / "link.pdf"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are not supported in this environment")

    assert run(source, dest, "--apply") == 0

    assert hidden.read_text() == "hidden"
    assert not target.exists()
    assert (dest / "documents" / "target.pdf").read_text() == "target"
    assert link.is_symlink()
    assert not (dest / "documents" / ".secret.pdf").exists()
    assert not (dest / "documents" / "link.pdf").exists()


def test_include_hidden_flag_moves_dotfile(tmp_path: Path) -> None:
    source = tmp_path / "inbox"
    dest = tmp_path / "organized"
    make_file(source / ".secret.pdf", "hidden")

    assert run(source, dest, "--apply", "--include-hidden") == 0
    assert (dest / "documents" / ".secret.pdf").read_text() == "hidden"


def test_include_symlinks_moves_link_without_following(tmp_path: Path) -> None:
    source = tmp_path / "inbox"
    dest = tmp_path / "organized"
    target = make_file(source / "target.pdf", "target")
    link = source / "link.pdf"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are not supported in this environment")

    assert run(source, dest, "--apply", "--include-symlinks") == 0

    assert not target.exists()
    moved_target = dest / "documents" / "target.pdf"
    moved_link = dest / "documents" / "link.pdf"
    assert moved_target.read_text() == "target"
    assert moved_link.is_symlink()
    # The link still points at the old source path; it was not followed.
    assert not moved_link.exists()


def test_refuses_log_file_inside_source(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "inbox"
    dest = tmp_path / "organized"
    make_file(source / "a.pdf")
    assert run(source, dest, "--apply", "--log-file", str(source / "audit.log")) == 2
    assert (source / "a.pdf").is_file()
    assert "log file must not be inside" in capsys.readouterr().err


def test_directories_are_not_moved(tmp_path: Path) -> None:
    source = tmp_path / "inbox"
    dest = tmp_path / "organized"
    nested = make_file(source / "keep" / "inside.txt", "nested")
    make_file(source / "loose.txt", "loose")

    assert run(source, dest, "--apply") == 0

    assert nested.read_text() == "nested"
    assert (dest / "documents" / "loose.txt").is_file()
    assert not (dest / "keep").exists()


def test_refuses_same_source_and_destination(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "inbox"
    make_file(source / "a.pdf")

    assert run(source, source, "--apply") == 2
    assert (source / "a.pdf").is_file()
    assert "REFUSING" in capsys.readouterr().err


def test_refuses_destination_inside_source(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "inbox"
    make_file(source / "a.pdf")

    assert run(source, source / "organized", "--apply") == 2
    assert (source / "a.pdf").is_file()
    assert not (source / "organized").exists()
    assert "inside source" in capsys.readouterr().err


def test_refuses_source_inside_destination(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dest = tmp_path / "organized"
    source = dest / "inbox"
    make_file(source / "a.pdf")

    assert run(source, dest, "--apply") == 2
    assert (source / "a.pdf").is_file()
    assert "must not overlap" in capsys.readouterr().err


def test_refuses_destination_that_is_a_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "inbox"
    make_file(source / "a.pdf")
    dest = make_file(tmp_path / "not_a_dir", "file")

    assert run(source, dest, "--apply") == 2
    assert "not a directory" in capsys.readouterr().err


def test_refuses_missing_source(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(tmp_path / "missing", tmp_path / "out", "--apply") == 2
    assert "does not exist" in capsys.readouterr().err


def test_refuses_root_source(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--source", "/", "--dest", str(tmp_path / "out")]) == 2
    assert "root" in capsys.readouterr().err


def test_source_is_not_touched_if_one_move_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A sanity test for the failure counter without touching any real path.
    import organizer.organize_files as organizer

    source = tmp_path / "inbox"
    dest = tmp_path / "organized"
    make_file(source / "a.pdf", "a")
    original_move = organizer.shutil.move

    def failing_move(src: str, dst: str) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(organizer.shutil, "move", failing_move)
    assert run(source, dest, "--apply") == 1
    assert (source / "a.pdf").is_file()
    monkeypatch.setattr(organizer.shutil, "move", original_move)
