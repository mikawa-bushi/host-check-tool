"""Tier-3 disk sampler: partition usage and watch-dir size scanning.

Two distinct collection modes live in the same class so the recorder can
drive them at different intervals (``disk_interval`` for partition snapshots,
``dir_scan_interval`` for directory walks) with a single sampler instance:

* ``collect`` — fast path (60 s default).  Calls ``psutil.disk_partitions``
  and ``psutil.disk_usage`` for every *real* filesystem (pseudo-fs excluded),
  keeping what a non-root process can still write (``avail``, df's Avail,
  which leaves out the blocks ext4 reserves for root) next to ``total`` and
  ``used``, plus ``os.statvfs`` for inode usage and the mount options for a
  read-only flag — "space left but cannot write" (inode exhaustion, or a filesystem
  error that remounted it read-only) is invisible in byte counts alone.
  A mount whose mount point is a file (a bind-mounted ``/etc/hosts`` in a
  container) is not a filesystem to watch and is skipped (``is_watched_mount``,
  which ``snapshot`` uses too).  Produces ``disk_samples`` rows.

* ``collect_dirs`` — slow path (1 h default).  Walks each ``WatchDir`` in
  ``config.sampling.watch_dirs`` with ``os.scandir`` and a monotonic time
  budget.  If the budget is exhausted before the walk completes, the
  accumulated size is stored with ``is_estimate=1``.  Symbolic links are
  never followed, and the walk stays on the filesystem the directory is on
  (``du -x``, compared by ``st_dev``): ``/proc``, ``/sys``, other disks, and
  whatever else has its own device number (a btrfs subvolume, a ZFS dataset, an
  NFS mount) below it are not counted.  ``PermissionError`` on an entry below
  the directory is silenced and traversal continues.  A directory that cannot
  be read at all (missing, permission denied, not mounted yet) is stored with
  ``size_bytes`` NULL, not as an empty directory, and each one is named in
  ``ctx.notes`` when it becomes unreadable.  Produces ``dir_samples`` rows.

The sampler is registered under the name ``"disk"`` so the recorder can look
it up via ``get_sampler("disk")``.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional, Tuple

import psutil

from ..core.sampler import Note, RunContext, SampleBatch, Sampler, register_sampler
from ..storage import make_row

# Filesystem types that carry no persistent data and should be excluded from
# disk-usage monitoring to avoid misleading "full" readings.
_PSEUDO_FS_TYPES = frozenset(
    {
        "tmpfs",
        "proc",
        "sysfs",
        "devtmpfs",
        "devpts",
        "cgroup",
        "cgroup2",
        "pstore",
        "securityfs",
        "debugfs",
        "tracefs",
        "mqueue",
        "hugetlbfs",
        "bpf",
        "fusectl",
        "configfs",
        "selinuxfs",
        "squashfs",
        "overlay",
        "ramfs",
        "efivarfs",
        "autofs",
    }
)


@register_sampler
class DiskSampler(Sampler):
    """Tier-3 sampler: disk partition usage and directory size walks.

    Partition snapshots are collected via ``collect`` at the interval
    configured in ``sampling.disk_interval``.  Directory walks are performed
    by the recorder at ``sampling.dir_scan_interval`` by calling
    ``collect_dirs`` directly.
    """

    name = "disk"
    title = "ディスク使用量・ディレクトリサイズ"
    tier = "T3"
    interval_key = "disk_interval"
    requires_root = False
    what = "パーティション使用量とwatch_dirsのサイズを定期記録する"
    why = "ディスク満杯はサービス停止の直接原因となるため、外挿して残り日数を事前に警告するため"
    manual_commands = [
        "df -h",
        "du -sh /var/log/*",
    ]
    _unreadable: frozenset[str] = frozenset()

    def collect(self, ctx: RunContext) -> list[SampleBatch]:
        """Collect disk_usage for all non-pseudo-fs mount points."""
        ts = time.time()
        rows: list[tuple] = []

        try:
            partitions = psutil.disk_partitions(all=False)
        except Exception:
            partitions = []

        for part in partitions:
            if not is_watched_mount(part):
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except OSError:  # incl. PermissionError
                continue
            inodes_total, inodes_used = _inode_usage(part.mountpoint)
            rows.append(
                make_row(
                    "disk_samples",
                    ts=ts,
                    mountpoint=part.mountpoint,
                    total=usage.total,
                    used=usage.used,
                    avail=usage.free,
                    inodes_total=inodes_total,
                    inodes_used=inodes_used,
                    read_only=int(is_read_only(part.opts)),
                )
            )

        return [SampleBatch(table="disk_samples", rows=rows)]

    def collect_dirs(self, ctx: RunContext) -> list[SampleBatch]:
        """Walk each WatchDir and record its total size.

        A monotonic time budget per directory prevents a single large tree
        from stalling the sampler.  When the budget is exceeded the walk is
        cut short and the accumulated value is flagged with ``is_estimate=1``.
        """
        ts = time.time()
        rows: list[tuple] = []

        watch_dirs = ctx.config.sampling.watch_dirs
        unreadable: list[str] = []
        for wd in watch_dirs:
            size_bytes, is_estimate = _scan_dir(wd.path, wd.time_budget_sec)
            if size_bytes is None:
                unreadable.append(wd.path)
            rows.append(
                make_row(
                    "dir_samples",
                    ts=ts,
                    path=wd.path,
                    size_bytes=size_bytes,
                    is_estimate=int(is_estimate),
                )
            )

        self._note_unreadable(ctx, unreadable)
        return [SampleBatch(table="dir_samples", rows=rows)]

    def _note_unreadable(self, ctx: RunContext, unreadable: list[str]) -> None:
        """Name each watch dir that cannot be read, once each time it becomes unreadable.

        The recorder keeps one note per key, so each directory has its own.
        """
        ctx.notes.extend(
            Note(
                f"disk_dirs {path}",
                f"disk_dirs {path}: 監視ディレクトリを読めないため、サイズを記録できません"
                " (存在しない・権限がないなど)",
            )
            for path in unreadable
            if path not in self._unreadable
        )
        self._unreadable = frozenset(unreadable)


def is_watched_mount(part: Any) -> bool:
    """Whether a ``psutil.disk_partitions`` entry is a filesystem to watch.

    Not a pseudo filesystem, not one with an empty type (some virtual mounts),
    and mounted on a directory: a file mount point is a bind-mounted file such
    as a container's ``/etc/hosts``.
    """
    return (
        bool(part.fstype)
        and part.fstype.lower() not in _PSEUDO_FS_TYPES
        and os.path.isdir(part.mountpoint)
    )


def is_read_only(mount_opts: str) -> bool:
    """Whether a mount-options string (``"rw,relatime"``) says read-only."""
    return "ro" in (mount_opts or "").split(",")


def _inode_usage(mountpoint: str) -> Tuple[Optional[int], Optional[int]]:
    """Return ``(inodes_total, inodes_used)``; ``(None, None)`` if unknown.

    Filesystems without a fixed inode table (btrfs, some FUSE/network mounts)
    report ``f_files == 0``; that means "not applicable", not "full".
    """
    try:
        st = os.statvfs(mountpoint)
    except OSError:
        return None, None
    if st.f_files <= 0:
        return None, None
    return st.f_files, st.f_files - st.f_ffree


def _scan_dir(root: str, time_budget_sec: float) -> tuple[Optional[int], bool]:
    """Recursively sum file sizes under *root* within a time budget.

    Returns ``(total_bytes, is_estimate)`` where ``is_estimate`` is ``True``
    when the walk was aborted before completion.  ``total_bytes`` is ``None``
    when *root* itself cannot be read (missing, not a directory, permission
    denied): that is unknown, not an empty directory.  Symbolic links are not
    followed and the walk does not leave the filesystem *root* is on.
    ``PermissionError`` on an individual entry below *root* is silenced.
    """
    deadline = time.monotonic() + time_budget_sec
    try:
        root_dev = os.stat(root).st_dev
    except OSError:
        return None, False
    total = 0
    is_estimate = False

    stack: list[str] = [root]
    while stack:
        if time.monotonic() >= deadline:
            is_estimate = True
            break
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if time.monotonic() >= deadline:
                        is_estimate = True
                        break
                    try:
                        # Use lstat to avoid following symlinks.
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:  # incl. PermissionError
                        continue
                    if stat.st_dev != root_dev:
                        continue
                    if entry.is_symlink():
                        # Count the symlink itself but do not follow it.
                        total += stat.st_size
                    elif entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    else:
                        total += stat.st_size
        except OSError:  # incl. PermissionError
            if current == root:
                return None, False
            continue

    return total, is_estimate
