#!/usr/bin/env python3
"""Reuse or create a local, read-only snapshot of a Quicken SQLite export."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

CACHE_VERSION = 1
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def default_cache_dir() -> Path:
    """Return a persistent cache directory on the local macOS filesystem."""
    override = os.environ.get("QUICKEN_SQLITE_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "quicken-sqlite-query"
    if os.environ.get("XDG_CACHE_HOME"):
        return Path(os.environ["XDG_CACHE_HOME"]).expanduser() / "quicken-sqlite-query"
    return Path.home() / ".cache" / "quicken-sqlite-query"


def _stat_signature(path: Path) -> dict[str, int | bool]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"exists": False}
    return {
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def source_signature(source: Path) -> dict[str, object]:
    """Capture the freshness fields needed for a safe cache decision."""
    stat = source.stat()
    return {
        # The main freshness contract is source mtime + size.
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        # WAL/SHM and rollback-journal changes can leave the main file
        # unchanged, so include their signatures when they exist.
        "sidecars": {
            suffix: _stat_signature(source.with_name(source.name + suffix))
            for suffix in SIDECAR_SUFFIXES
        },
    }


def _has_live_sidecar(signature: dict[str, object]) -> bool:
    sidecars = signature["sidecars"]
    assert isinstance(sidecars, dict)
    return any(value.get("exists") for value in sidecars.values())


def _cache_paths(source: Path, cache_dir: Path) -> tuple[Path, Path, Path]:
    key = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:32]
    stem = source.stem or "sqlite"
    cache = cache_dir / f"{stem}-{key}.sqlite"
    return (
        cache,
        cache.with_name(cache.name + ".json"),
        cache.with_name(cache.name + ".lock"),
    )


def _read_manifest(manifest_path: Path) -> dict[str, object] | None:
    try:
        with manifest_path.open(encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return manifest if isinstance(manifest, dict) else None


def _cache_is_fresh(
    source: Path,
    cache: Path,
    manifest_path: Path,
    signature: dict[str, object],
) -> bool:
    manifest = _read_manifest(manifest_path)
    return bool(
        cache.is_file()
        and manifest
        and manifest.get("version") == CACHE_VERSION
        and manifest.get("source") == str(source)
        and manifest.get("source_signature") == signature
    )


def _read_only_uri(path: Path, *, immutable: bool) -> str:
    options = "mode=ro"
    if immutable:
        options += "&immutable=1"
    return f"file:{quote(str(path), safe='/:')}?{options}"


def _validate_snapshot(path: Path) -> None:
    """Ensure the snapshot is a complete SQLite database without writing it."""
    try:
        with sqlite3.connect(_read_only_uri(path, immutable=True), uri=True) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as error:
        raise RuntimeError(f"cached SQLite snapshot is not readable: {error}") from error
    if result != ("ok",):
        raise RuntimeError(f"cached SQLite snapshot failed quick_check: {result!r}")


def _copy_snapshot(source: Path, destination: Path) -> None:
    with (
        source.open("rb") as source_handle,
        destination.open("wb") as destination_handle,
    ):
        shutil.copyfileobj(source_handle, destination_handle, length=1024 * 1024)
        destination_handle.flush()
        os.fsync(destination_handle.fileno())


def _backup_snapshot(source: Path, destination: Path) -> None:
    # Do not use immutable=1 here: SQLite must be allowed to read committed
    # pages from a live WAL while the source connection remains read-only.
    with (
        sqlite3.connect(_read_only_uri(source, immutable=False), uri=True) as source_db,
        sqlite3.connect(destination) as destination_db,
    ):
        source_db.backup(destination_db)
        destination_db.commit()


def _write_manifest(path: Path, source: Path, signature: dict[str, object]) -> None:
    manifest = {
        "version": CACHE_VERSION,
        "source": str(source),
        "source_signature": signature,
    }
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=path.name + ".tmp-", delete=False
    ) as handle:
        temporary = Path(handle.name)
        json.dump(manifest, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _temporary_path(cache: Path, cache_dir: Path) -> Path:
    with tempfile.NamedTemporaryFile(
        dir=cache_dir, prefix=cache.name + ".tmp-", delete=False
    ) as handle:
        return Path(handle.name)


@contextlib.contextmanager
def _cache_lock(lock_path: Path):
    """Serialize refreshes; the cache remains safe even if a process dies."""
    lock_path.touch(mode=0o600, exist_ok=True)
    with lock_path.open("r+") as handle:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - only relevant on Windows
            fcntl = None
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ensure_cached(source: Path, cache_dir: Path, *, retries: int = 3) -> tuple[Path, bool]:
    """Return a validated local snapshot, refreshing it when stale."""
    source = source.expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError(f"source is not a regular file: {source}")
    if retries < 1:
        raise ValueError("retries must be at least 1")
    cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(cache_dir, 0o700)
    cache, manifest_path, lock_path = _cache_paths(source, cache_dir)

    with _cache_lock(lock_path):
        signature = source_signature(source)
        if _cache_is_fresh(source, cache, manifest_path, signature):
            return cache, False

        for attempt in range(retries):
            before = source_signature(source)
            temporary = _temporary_path(cache, cache_dir)
            try:
                if _has_live_sidecar(before):
                    _backup_snapshot(source, temporary)
                else:
                    _copy_snapshot(source, temporary)
                os.chmod(temporary, 0o600)
                _validate_snapshot(temporary)
                after = source_signature(source)
                if before != after:
                    if attempt + 1 < retries:
                        time.sleep(0.05 * (attempt + 1))
                        continue
                    raise RuntimeError(
                        "source SQLite file changed while it was being snapshotted; "
                        "retry after the export is quiescent"
                    )

                # Replace the database before its manifest. If interrupted
                # between these operations, an old manifest can only cause a
                # conservative refresh, never acceptance of a partial file.
                os.replace(temporary, cache)
                _write_manifest(manifest_path, source, after)
                return cache, True
            finally:
                temporary.unlink(missing_ok=True)

    raise AssertionError("unreachable")


def main() -> int:
    """Cache the source SQLite database named on the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="source SQLite database")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=default_cache_dir(),
        help="local persistent cache directory",
    )
    args = parser.parse_args()
    cache, refreshed = ensure_cached(args.source, args.cache_dir)
    print(cache)
    print("refreshed" if refreshed else "reused", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
