import ctypes
import errno
import hashlib
import json
import os
import stat
import sys
from pathlib import Path


class UnsafeInput(Exception):
    pass


class AmbiguousDelivery(Exception):
    pass


def sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def regular_fd(path: Path) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise UnsafeInput("not a regular file")
    return fd


def fingerprint(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with os.fdopen(regular_fd(path), "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path: Path, data: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    sync_dir(path.parent)


def rename_noreplace(source: Path, destination: Path) -> None:
    """An exclusive atomic rename; never a check-then-overwriting os.rename fallback."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "linux":
        result = libc.renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    elif sys.platform == "darwin":
        result = libc.renamex_np(os.fsencode(source), os.fsencode(destination), 4)
    else:
        raise OSError(errno.ENOTSUP, "exclusive rename is unsupported")
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    sync_dir(destination.parent)
    if source.parent != destination.parent:
        sync_dir(source.parent)


def copy_source(source: Path, destination: Path, max_bytes: int) -> tuple[str, tuple]:
    h = hashlib.sha256()
    with os.fdopen(regular_fd(source), "rb") as src:
        before = fingerprint(os.fstat(src.fileno()))
        if before[2] > max_bytes:
            raise UnsafeInput("source exceeds configured byte limit")
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as dst:
            total = 0
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                total += len(chunk)
                if total > max_bytes:
                    raise UnsafeInput("source grew beyond configured byte limit")
                dst.write(chunk)
                h.update(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        after = fingerprint(os.fstat(src.fileno()))
    if before != after or fingerprint(source.lstat()) != before:
        raise UnsafeInput("source mutated during copy")
    sync_dir(destination.parent)
    return h.hexdigest(), before


def verify_layout(config) -> None:
    config.validate()
    roots = []
    for name in (
        "inbox",
        "consume",
        "archive",
        "review",
        "staging",
        "work",
        "state",
        "model_cache",
    ):
        path = getattr(config, name)
        if not path.is_dir():
            raise ValueError(f"{name} must be a pre-created directory")
        roots.append(path)
    if len({(root.stat().st_dev, root.stat().st_ino) for root in roots}) != len(roots):
        raise ValueError("configured roots alias the same physical directory")
    if config.staging.stat().st_dev != config.consume.stat().st_dev:
        raise ValueError("staging and consume must share a filesystem")
    if not os.access(config.consume, os.W_OK | os.X_OK):
        raise ValueError("consume is not writable by the runtime UID/GID")
    for name in ("archive", "review", "staging", "work", "state", "model_cache"):
        root = getattr(config, name)
        if not os.access(root, os.W_OK | os.X_OK):
            raise ValueError(f"{name} is not writable by the runtime UID/GID")
    if sys.platform == "linux":
        mounts = []
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            left, right = line.split(" - ", 1)
            mount = Path(left.split()[4].replace("\\040", " "))
            mounts.append(
                (
                    mount,
                    left.split()[0],
                    right.split()[0],
                    left.split()[2],
                    Path(left.split()[3].replace("\\040", " ")),
                )
            )

        def containing(path):
            return max(
                (row for row in mounts if path == row[0] or row[0] in path.parents),
                key=lambda row: len(row[0].parts),
            )

        # Separate bind mounts can return EXDEV even with equal st_dev.
        if containing(config.staging)[1] != containing(config.consume)[1]:
            raise ValueError("staging and consume require ONE common parent bind mount")
        if containing(config.state)[2] not in {"ext4", "xfs", "btrfs", "zfs", "overlay"}:
            raise ValueError("state must use a supported local filesystem, not NFS/CIFS/FUSE")
        if containing(config.consume)[2] not in {"ext4", "xfs", "btrfs", "zfs", "overlay"}:
            raise ValueError(
                "handoff requires a local filesystem, not an Unraid user-share/FUSE path"
            )
        physical = []
        for root in roots:
            mount, _, _, device, subroot = containing(root)
            location = subroot / root.relative_to(mount)
            for other_device, other in physical:
                if device == other_device and (
                    location == other or location in other.parents or other in location.parents
                ):
                    raise ValueError("configured roots overlap through host bind mounts")
            physical.append((device, location))
