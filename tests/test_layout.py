from pathlib import Path

import pytest

from doc_splitter.fs import verify_layout


def mount_table(monkeypatch, rows):
    original = Path.read_text

    def read(path, *args, **kwargs):
        if str(path) == "/proc/self/mountinfo":
            return "\n".join(rows)
        return original(path, *args, **kwargs)

    monkeypatch.setattr("doc_splitter.fs.sys.platform", "linux")
    monkeypatch.setattr(Path, "read_text", read)


def test_separate_binds_rejected_even_on_same_device(config, monkeypatch):
    mount_table(
        monkeypatch,
        [
            "1 0 0:1 / / rw - ext4 /dev/test rw",
            f"2 1 0:1 /host/staging {config.staging} rw - ext4 /dev/test rw",
            f"3 1 0:1 /host/consume {config.consume} rw - ext4 /dev/test rw",
        ],
    )
    with pytest.raises(ValueError, match="ONE common"):
        verify_layout(config)


def test_host_bind_overlap_rejected(config, monkeypatch):
    mount_table(
        monkeypatch,
        [
            "1 0 0:1 / / rw - ext4 /dev/test rw",
            f"2 1 0:2 /payload {config.archive} rw - ext4 /dev/other rw",
            f"3 1 0:2 /payload/work {config.work} rw - ext4 /dev/other rw",
        ],
    )
    with pytest.raises(ValueError, match="host bind mounts"):
        verify_layout(config)


@pytest.mark.parametrize("root", ["state", "consume"])
@pytest.mark.parametrize("filesystem", ["nfs", "cifs", "fuse.shfs", "tmpfs"])
def test_unsupported_filesystems_rejected(config, monkeypatch, root, filesystem):
    rows = ["1 0 0:1 / / rw - ext4 /dev/test rw"]
    if root == "state":
        rows.append(f"2 1 0:2 / {config.state} rw - {filesystem} /test rw")
    else:
        # The common parent covers both consume and staging.
        rows = [f"1 0 0:1 / / rw - {filesystem} /test rw"]
        rows.append(f"2 1 0:2 / {config.state} rw - ext4 /dev/test rw")
    mount_table(monkeypatch, rows)
    with pytest.raises(ValueError, match="filesystem|FUSE"):
        verify_layout(config)


def test_valid_local_layout(config, monkeypatch):
    mount_table(monkeypatch, ["1 0 0:1 / / rw - ext4 /dev/test rw"])
    verify_layout(config)
