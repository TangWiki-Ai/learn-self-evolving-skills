"""Descriptor-anchored filesystem primitives for protected test assets."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Literal


@dataclass(frozen=True, slots=True)
class RegularFileSnapshot:
    data: bytes
    mode: int


def _lexical_absolute(path: Path) -> Path:
    if ".." in path.parts:
        raise ValueError("secure path cannot contain parent traversal")
    lexical = path if path.is_absolute() else Path.cwd() / path
    if not lexical.anchor:
        raise ValueError("secure path must have a filesystem anchor")
    return lexical


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_directory_path(path: Path) -> int:
    lexical = _lexical_absolute(path)
    descriptor: int | None = None
    try:
        descriptor = os.open(lexical.anchor, _directory_flags())
        for part in lexical.parts[1:]:
            next_descriptor = os.open(
                part,
                _directory_flags(),
                dir_fd=descriptor,
            )
            previous_descriptor = descriptor
            descriptor = next_descriptor
            os.close(previous_descriptor)
        return descriptor
    except OSError as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise ValueError(
            "secure path has a symlink ancestor or unreadable directory"
        ) from exc


def _open_or_create_empty_directory_path(path: Path) -> int:
    lexical = _lexical_absolute(path)
    descriptor: int | None = None
    try:
        descriptor = os.open(lexical.anchor, _directory_flags())
        for part in lexical.parts[1:]:
            try:
                next_descriptor = os.open(
                    part,
                    _directory_flags(),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                next_descriptor = os.open(
                    part,
                    _directory_flags(),
                    dir_fd=descriptor,
                )
            previous_descriptor = descriptor
            descriptor = next_descriptor
            os.close(previous_descriptor)
        with os.scandir(descriptor) as entries:
            if next(entries, None) is not None:
                raise FileExistsError("secure output directory is not empty")
        os.fchmod(descriptor, 0o700)
        return descriptor
    except OSError as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if isinstance(exc, FileExistsError):
            raise
        raise ValueError(
            "secure output path has a symlink ancestor or unreadable directory"
        ) from exc


class SecureDirectoryWriter:
    """Write one new private tree through a root descriptor held for its lifetime."""

    __slots__ = ("_closed", "_root", "_root_descriptor")

    def __init__(self, root: Path, root_descriptor: int) -> None:
        self._root = root if root.is_absolute() else Path.cwd() / root
        self._root_descriptor = root_descriptor
        self._closed = False

    @classmethod
    def create(cls, root: Path) -> SecureDirectoryWriter:
        return cls(root, _open_or_create_empty_directory_path(root))

    def __enter__(self) -> SecureDirectoryWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        verification_error: ValueError | None = None
        try:
            os.fsync(self._root_descriptor)
            try:
                reopened = _open_directory_path(self._root)
            except ValueError as exc:
                verification_error = ValueError(
                    "secure output root path changed during bundle construction"
                )
                verification_error.__cause__ = exc
            else:
                try:
                    expected = os.fstat(self._root_descriptor)
                    observed = os.fstat(reopened)
                    if (expected.st_dev, expected.st_ino) != (
                        observed.st_dev,
                        observed.st_ino,
                    ):
                        verification_error = ValueError(
                            "secure output root path changed during bundle construction"
                        )
                finally:
                    os.close(reopened)
        finally:
            os.close(self._root_descriptor)
            self._closed = True
        if exc_type is None and verification_error is not None:
            raise verification_error
        return False

    @staticmethod
    def _parts(relative: str) -> tuple[str, ...]:
        path = PurePosixPath(relative)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise ValueError("secure output file path is unsafe")
        return path.parts

    def _open_parent(self, parts: tuple[str, ...]) -> int:
        if self._closed:
            raise ValueError("secure output writer is closed")
        descriptor = os.dup(self._root_descriptor)
        try:
            for part in parts:
                try:
                    next_descriptor = os.open(
                        part,
                        _directory_flags(),
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                    next_descriptor = os.open(
                        part,
                        _directory_flags(),
                        dir_fd=descriptor,
                    )
                if stat.S_IMODE(os.fstat(next_descriptor).st_mode) != 0o700:
                    os.close(next_descriptor)
                    raise ValueError("secure output directory permissions must be 0700")
                previous_descriptor = descriptor
                descriptor = next_descriptor
                os.close(previous_descriptor)
            return descriptor
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise

    def write_bytes(self, relative: str, value: bytes) -> str:
        parts = self._parts(relative)
        parent_descriptor = self._open_parent(parts[:-1])
        file_descriptor: int | None = None
        created = False
        try:
            file_descriptor = os.open(
                parts[-1],
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=parent_descriptor,
            )
            created = True
            os.fchmod(file_descriptor, 0o600)
            remaining = memoryview(value)
            while remaining:
                written = os.write(file_descriptor, remaining)
                if written <= 0:
                    raise OSError("secure output write made no progress")
                remaining = remaining[written:]
            os.fsync(file_descriptor)
            return hashlib.sha256(value).hexdigest()
        except BaseException:
            if created:
                try:
                    os.unlink(parts[-1], dir_fd=parent_descriptor)
                except OSError:
                    pass
            raise
        finally:
            if file_descriptor is not None:
                try:
                    os.close(file_descriptor)
                except OSError:
                    pass
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)


class SecureDirectorySnapshot:
    """Cache one descriptor-bound regular tree for a validation lifecycle."""

    __slots__ = (
        "_files",
        "_root",
        "_root_descriptor",
        "_root_mode",
        "_tree_identity",
    )

    def __init__(self, root: Path, root_descriptor: int) -> None:
        self._root = root if root.is_absolute() else Path.cwd() / root
        self._root_descriptor = root_descriptor
        self._root_mode = stat.S_IMODE(os.fstat(root_descriptor).st_mode)
        self._tree_identity, self._files = _capture_directory_tree(
            root_descriptor,
            include_data=True,
        )

    @classmethod
    def open(cls, root: Path) -> SecureDirectorySnapshot:
        return cls(root, _open_directory_path(root))

    def __enter__(self) -> SecureDirectorySnapshot:
        return self

    @staticmethod
    def _relative(relative: str) -> str:
        path = PurePosixPath(relative)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise ValueError("secure validation file path is unsafe")
        return path.as_posix()

    def file(self, relative: str) -> RegularFileSnapshot:
        try:
            return self._files[self._relative(relative)]
        except KeyError:
            raise ValueError("secure validation file is missing") from None

    @property
    def file_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._files))

    def require_private_modes(self) -> None:
        if self._root_mode != 0o700:
            raise ValueError("external holdout bundle root permissions must be 0700")
        for identity in self._tree_identity.values():
            kind, _, _, mode, _, _ = identity
            if kind == "directory" and mode != 0o700:
                raise ValueError(
                    "external holdout bundle directory permissions must be 0700"
                )
            if kind == "file" and mode != 0o600:
                raise ValueError(
                    "external holdout bundle file permissions must be 0600"
                )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        verification_error: ValueError | None = None
        try:
            try:
                observed_tree = _directory_tree_identity(self._root_descriptor)
            except ValueError as exc:
                verification_error = ValueError(
                    "secure validation directory tree changed during validation"
                )
                verification_error.__cause__ = exc
            else:
                if observed_tree != self._tree_identity:
                    verification_error = ValueError(
                        "secure validation directory tree changed during validation"
                    )
            try:
                reopened = _open_directory_path(self._root)
            except ValueError as exc:
                verification_error = ValueError(
                    "secure validation root path changed during validation"
                )
                verification_error.__cause__ = exc
            else:
                try:
                    expected = os.fstat(self._root_descriptor)
                    observed = os.fstat(reopened)
                    if (expected.st_dev, expected.st_ino) != (
                        observed.st_dev,
                        observed.st_ino,
                    ):
                        verification_error = ValueError(
                            "secure validation root path changed during validation"
                        )
                finally:
                    os.close(reopened)
        finally:
            os.close(self._root_descriptor)
        if exc_type is None and verification_error is not None:
            raise verification_error
        return False


def _capture_directory_tree(
    directory_descriptor: int,
    prefix: str = "",
    *,
    include_data: bool,
) -> tuple[
    dict[str, tuple[str, int, int, int, int, int]],
    dict[str, RegularFileSnapshot],
]:
    try:
        with os.scandir(directory_descriptor) as entries:
            names = sorted(entry.name for entry in entries)
    except OSError as exc:
        raise ValueError("secure validation directory cannot be scanned") from exc
    identity: dict[str, tuple[str, int, int, int, int, int]] = {}
    files: dict[str, RegularFileSnapshot] = {}
    for name in names:
        child_descriptor: int | None = None
        try:
            child_descriptor = os.open(
                name,
                os.O_RDONLY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_descriptor,
            )
            child_stat = os.fstat(child_descriptor)
            relative = f"{prefix}/{name}" if prefix else name
            if stat.S_ISDIR(child_stat.st_mode):
                kind = "directory"
                child_identity, child_files = _capture_directory_tree(
                    child_descriptor,
                    relative,
                    include_data=include_data,
                )
                identity.update(child_identity)
                files.update(child_files)
            elif stat.S_ISREG(child_stat.st_mode):
                kind = "file"
                if include_data:
                    chunks: list[bytes] = []
                    while chunk := os.read(child_descriptor, 1024 * 1024):
                        chunks.append(chunk)
                    files[relative] = RegularFileSnapshot(
                        data=b"".join(chunks),
                        mode=stat.S_IMODE(child_stat.st_mode),
                    )
            else:
                raise ValueError("secure validation tree contains a non-regular entry")
            identity[relative] = (
                kind,
                child_stat.st_dev,
                child_stat.st_ino,
                stat.S_IMODE(child_stat.st_mode),
                child_stat.st_size,
                child_stat.st_mtime_ns,
            )
        except OSError as exc:
            raise ValueError(
                "secure validation tree contains a symlink or unreadable entry"
            ) from exc
        finally:
            if child_descriptor is not None:
                try:
                    os.close(child_descriptor)
                except OSError:
                    pass
    return identity, files


def _directory_tree_identity(
    directory_descriptor: int,
) -> dict[str, tuple[str, int, int, int, int, int]]:
    identity, _ = _capture_directory_tree(
        directory_descriptor,
        include_data=False,
    )
    return identity


def read_regular_file_snapshot(root: Path, relative: str) -> RegularFileSnapshot:
    """Read one regular file through directory descriptors anchored at ``root``."""

    relative_path = PurePosixPath(relative)
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or ".." in relative_path.parts
    ):
        raise ValueError("secure file path is unsafe")
    directory_descriptor: int | None = None
    file_descriptor: int | None = None
    try:
        directory_descriptor = _open_directory_path(root)
        for part in relative_path.parts[:-1]:
            next_descriptor = os.open(
                part,
                _directory_flags(),
                dir_fd=directory_descriptor,
            )
            previous_descriptor = directory_descriptor
            directory_descriptor = next_descriptor
            os.close(previous_descriptor)
        file_descriptor = os.open(
            relative_path.parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_descriptor,
        )
        file_stat = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("secure file is not a regular file")
        chunks: list[bytes] = []
        while chunk := os.read(file_descriptor, 1024 * 1024):
            chunks.append(chunk)
        return RegularFileSnapshot(
            data=b"".join(chunks),
            mode=stat.S_IMODE(file_stat.st_mode),
        )
    except OSError as exc:
        raise ValueError(
            "secure file path has a symlink ancestor or unreadable component"
        ) from exc
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if directory_descriptor is not None:
            try:
                os.close(directory_descriptor)
            except OSError:
                pass


def _validate_private_tree_children(directory_descriptor: int) -> None:
    try:
        with os.scandir(directory_descriptor) as entries:
            names = sorted(entry.name for entry in entries)
    except OSError as exc:
        raise ValueError("external holdout bundle directory cannot be scanned") from exc
    for name in names:
        child_descriptor: int | None = None
        try:
            child_descriptor = os.open(
                name,
                os.O_RDONLY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_descriptor,
            )
            child_stat = os.fstat(child_descriptor)
            child_mode = stat.S_IMODE(child_stat.st_mode)
            if stat.S_ISDIR(child_stat.st_mode):
                if child_mode != 0o700:
                    raise ValueError(
                        "external holdout bundle directory permissions must be 0700"
                    )
                _validate_private_tree_children(child_descriptor)
            elif stat.S_ISREG(child_stat.st_mode):
                if child_mode != 0o600:
                    raise ValueError(
                        "external holdout bundle file permissions must be 0600"
                    )
            else:
                raise ValueError(
                    "external holdout bundle may contain only regular files and directories"
                )
        except OSError as exc:
            raise ValueError(
                "external holdout bundle contains a symlink or unreadable entry"
            ) from exc
        finally:
            if child_descriptor is not None:
                try:
                    os.close(child_descriptor)
                except OSError:
                    pass


def validate_private_tree_permissions(root: Path) -> None:
    """Require an owner-only regular directory tree without following symlinks."""

    root_descriptor: int | None = None
    try:
        root_descriptor = _open_directory_path(root)
        if stat.S_IMODE(os.fstat(root_descriptor).st_mode) != 0o700:
            raise ValueError("external holdout bundle root permissions must be 0700")
        _validate_private_tree_children(root_descriptor)
    finally:
        if root_descriptor is not None:
            try:
                os.close(root_descriptor)
            except OSError:
                pass
