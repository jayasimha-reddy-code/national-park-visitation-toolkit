#!/usr/bin/env python3
"""Safely group loose files into type-based folders.

The default is a dry run: nothing is created, moved, or renamed until
``--apply`` is supplied.  Collisions never overwrite an existing file; a
numbered name such as ``report_1.pdf`` is selected instead.

Example:
    python organizer/organize_files.py --source ~/inbox --dest ~/organized
    python organizer/organize_files.py --source ~/inbox --dest ~/organized --apply
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

LOG = logging.getLogger("organize_files")
LOG_FILENAME = "organize_files.log"

CATEGORY_EXTENSIONS: dict[str, set[str]] = {
    "documents": {
        ".doc", ".docx", ".epub", ".md", ".odt", ".pages", ".pdf", ".rtf",
        ".tex", ".txt", ".wpd",
    },
    "images": {
        ".avif", ".bmp", ".gif", ".heic", ".heif", ".ico", ".jpeg", ".jpg",
        ".png", ".svg", ".tif", ".tiff", ".webp",
    },
    "data": {
        ".arrow", ".csv", ".db", ".feather", ".geojson", ".json", ".jsonl",
        ".ods", ".parquet", ".sav", ".sqlite", ".tsv", ".xls", ".xlsx",
        ".xml", ".yaml", ".yml",
    },
    "archives": {
        ".7z", ".bz2", ".gz", ".rar", ".tar", ".tgz", ".xz", ".zip",
    },
}
CATEGORIES = tuple(CATEGORY_EXTENSIONS) + ("other",)
EXTENSION_TO_CATEGORY = {
    extension: category
    for category, extensions in CATEGORY_EXTENSIONS.items()
    for extension in extensions
}


class SafetyError(ValueError):
    """Raised when paths are unsafe to operate on."""


@dataclass(frozen=True)
class PlannedMove:
    source: Path
    category: str
    destination: Path


@dataclass
class Plan:
    moves: list[PlannedMove] = field(default_factory=list)
    skipped: dict[str, int] = field(
        default_factory=lambda: {
            "hidden": 0,
            "symlink": 0,
            "directory": 0,
            "special": 0,
        }
    )
    conflicts_renamed: int = 0

    def category_counts(self) -> dict[str, int]:
        counts = {category: 0 for category in CATEGORIES}
        for move in self.moves:
            counts[move.category] += 1
        return counts


def configure_logging(verbose: bool) -> None:
    """Configure console logging only (safe before path validation)."""
    LOG.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOG.propagate = False
    for handler in list(LOG.handlers):
        LOG.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z"
    )
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    LOG.addHandler(console)


def add_file_logging(log_file: Path) -> None:
    """Add an audit-log handler only after source/destination are validated."""
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z"
    )
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setFormatter(formatter)
    LOG.addHandler(file_handler)


def _is_root(path: Path) -> bool:
    return path.parent == path


def validate_paths(source: Path, destination: Path) -> tuple[Path, Path]:
    """Resolve and validate source/destination, refusing unsafe combinations."""
    raw_source = source.expanduser()
    raw_destination = destination.expanduser()

    if raw_source.is_symlink():
        raise SafetyError(f"source is a symlink, refusing to follow it: {raw_source}")
    if raw_destination.is_symlink():
        raise SafetyError(
            f"destination is a symlink, refusing to write through it: {raw_destination}"
        )
    if not raw_source.exists():
        raise SafetyError(f"source does not exist: {raw_source}")
    if not raw_source.is_dir():
        raise SafetyError(f"source is not a directory: {raw_source}")

    resolved_source = raw_source.resolve(strict=True)
    resolved_destination = raw_destination.resolve(strict=False)

    if not resolved_destination.exists():
        # resolve(strict=False) still gives an absolute path for a new folder.
        pass
    elif not resolved_destination.is_dir():
        raise SafetyError(
            f"destination exists but is not a directory: {resolved_destination}"
        )

    if _is_root(resolved_source) or _is_root(resolved_destination):
        raise SafetyError("filesystem root is never a safe source or destination")
    if resolved_source == Path.home().resolve():
        raise SafetyError("refusing to reorganize the entire home directory")
    if resolved_destination == Path.home().resolve():
        raise SafetyError("refusing to use the entire home directory as destination")

    if resolved_source == resolved_destination:
        raise SafetyError("source and destination are the same directory")
    if resolved_destination.is_relative_to(resolved_source):
        raise SafetyError(
            "destination is inside source; this would move files into a folder "
            "that is being scanned and is refused"
        )
    if resolved_source.is_relative_to(resolved_destination):
        raise SafetyError(
            "source is inside destination; source and destination must not overlap"
        )

    return resolved_source, resolved_destination


def _path_exists(path: Path) -> bool:
    """True for directories, files, and broken symlinks."""
    return os.path.lexists(path)


def category_for(path: Path) -> str:
    suffix = path.suffix.lower()
    return EXTENSION_TO_CATEGORY.get(suffix, "other")


def unique_destination(
    destination_dir: Path, filename: str, reserved: set[str]
) -> tuple[Path, bool]:
    """Return a non-existing destination and whether its name had to change."""
    base = Path(filename)
    stem, suffix = base.stem, base.suffix
    candidate_name = filename
    counter = 1
    changed = False

    while True:
        candidate = destination_dir / candidate_name
        if not _path_exists(candidate) and candidate_name.casefold() not in reserved:
            reserved.add(candidate_name.casefold())
            return candidate, changed
        changed = True
        candidate_name = f"{stem}_{counter}{suffix}"
        counter += 1
        if counter > 10_000:
            raise SafetyError(f"could not find a free numbered name for {filename!r}")


def _iter_top_level_items(
    source: Path, include_hidden: bool, include_symlinks: bool, plan: Plan
) -> Iterable[Path]:
    try:
        entries = sorted(source.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        raise SafetyError(f"cannot list source directory {source}: {exc}") from exc

    for entry in entries:
        if entry.name.startswith(".") and not include_hidden:
            plan.skipped["hidden"] += 1
            LOG.debug("SKIP hidden item: %s", entry)
            continue
        if entry.is_symlink():
            if not include_symlinks:
                plan.skipped["symlink"] += 1
                LOG.info("SKIP symlink (default): %s", entry)
                continue
            LOG.warning("Symlink will be moved, not followed: %s", entry)
            yield entry
            continue
        if entry.is_dir():
            plan.skipped["directory"] += 1
            LOG.debug("SKIP directory (no recursion): %s", entry)
            continue
        if not entry.is_file():
            plan.skipped["special"] += 1
            LOG.warning("SKIP special filesystem item: %s", entry)
            continue
        yield entry


def build_plan(
    source: Path,
    destination: Path,
    *,
    include_hidden: bool = False,
    include_symlinks: bool = False,
) -> Plan:
    plan = Plan()
    reserved: set[str] = set()

    for entry in _iter_top_level_items(source, include_hidden, include_symlinks, plan):
        category = category_for(entry)
        destination_dir = destination / category
        final_path, renamed = unique_destination(destination_dir, entry.name, reserved)
        if renamed:
            plan.conflicts_renamed += 1
        plan.moves.append(PlannedMove(entry, category, final_path))

    return plan


def print_summary(plan: Plan, destination: Path, apply: bool) -> None:
    mode = "APPLY" if apply else "DRY-RUN"
    LOG.info("%s summary for destination %s", mode, destination)
    counts = plan.category_counts()
    for category in CATEGORIES:
        LOG.info("  %-9s %d planned", category, counts[category])
    LOG.info("  collisions numbered: %d", plan.conflicts_renamed)
    LOG.info(
        "  skipped: hidden=%d symlink=%d directory=%d special=%d",
        plan.skipped["hidden"],
        plan.skipped["symlink"],
        plan.skipped["directory"],
        plan.skipped["special"],
    )
    LOG.info("  total moves planned: %d", len(plan.moves))


def apply_plan(plan: Plan, destination: Path, reserved: set[str]) -> tuple[int, int]:
    moved = 0
    failed = 0
    destination.mkdir(parents=True, exist_ok=True)

    for move in plan.moves:
        source = move.source
        target = move.destination

        if source.is_symlink():
            # An explicitly included symlink is moved as a link, never followed.
            pass
        elif not source.exists():
            LOG.error("SKIP missing source (changed during run): %s", source)
            failed += 1
            continue

        if _path_exists(target):
            # A file may have appeared after planning. Never overwrite it.
            target, renamed = unique_destination(
                target.parent, target.name, reserved
            )
            if renamed:
                LOG.warning("Collision appeared after planning; using %s", target)

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if _path_exists(target):
                raise SafetyError(f"refusing to overwrite {target}")
            shutil.move(str(source), str(target))
        except (OSError, SafetyError) as exc:
            LOG.error("FAILED %s -> %s: %s", source, target, exc)
            failed += 1
            continue

        moved += 1
        LOG.info("MOVED %s -> %s", source, target)

    return moved, failed


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Group top-level files into documents/, images/, data/, archives/, "
            "and other/. Dry-run by default; pass --apply to move files."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--source", type=Path, required=True, help="folder to read")
    parser.add_argument(
        "--dest", type=Path, required=True, help="folder to create category subfolders in"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually move files (without this flag nothing is changed)",
    )
    parser.add_argument(
        "--include-hidden",
        action="store_true",
        help="include dotfiles/dot-directories (hidden entries are skipped by default)",
    )
    parser.add_argument(
        "--include-symlinks",
        action="store_true",
        help="move symlinks themselves; targets are never followed (default: skip)",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help=(
            "append an audit log here; on --apply the default is "
            f"<dest>/{LOG_FILENAME}; dry runs log to stderr only"
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="show debug messages")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    configure_logging(args.verbose)

    try:
        source, destination = validate_paths(args.source, args.dest)
    except SafetyError as exc:
        LOG.error("REFUSING TO RUN: %s", exc)
        return 2
    except OSError as exc:
        LOG.error("REFUSING TO RUN: cannot resolve paths: %s", exc)
        return 2

    log_file: Path | None = None
    if args.apply:
        log_file = (
            args.log_file.expanduser()
            if args.log_file
            else destination / LOG_FILENAME
        )
        resolved_log = log_file.resolve(strict=False)
        if resolved_log.is_relative_to(source):
            LOG.error(
                "REFUSING TO RUN: log file must not be inside the source directory"
            )
            return 2
        try:
            add_file_logging(log_file)
        except OSError as exc:
            LOG.error("REFUSING TO RUN: cannot open log file %s: %s", log_file, exc)
            return 2

    LOG.info("Mode: %s", "APPLY (files will be moved)" if args.apply else "DRY-RUN")
    LOG.info("Source: %s", source)
    LOG.info("Destination: %s", destination)

    try:
        plan = build_plan(
            source,
            destination,
            include_hidden=args.include_hidden,
            include_symlinks=args.include_symlinks,
        )
    except SafetyError as exc:
        LOG.error("REFUSING TO RUN: %s", exc)
        return 2

    for move in plan.moves:
        verb = "MOVE" if args.apply else "WOULD MOVE"
        LOG.info(
            "%s [%s] %s -> %s", verb, move.category, move.source, move.destination
        )

    print_summary(plan, destination, args.apply)

    if not args.apply:
        LOG.info("Dry run complete. Re-run with --apply to perform these moves.")
        return 0

    # Keep the renamed names reserved across planning and applying.
    reserved = {move.destination.name.casefold() for move in plan.moves}
    try:
        moved, failed = apply_plan(plan, destination, reserved)
    except OSError as exc:
        LOG.error("Could not create/use destination %s: %s", destination, exc)
        return 2
    LOG.info("Applied: %d moved, %d failed", moved, failed)
    LOG.info("Audit log: %s", log_file)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
