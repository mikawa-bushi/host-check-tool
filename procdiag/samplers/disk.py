"""Tier-3 disk sampler: partition usage and watch-dir size scanning.

Two distinct collection modes live in the same class so the recorder can
drive them at different intervals (``disk_interval`` for partition snapshots,
``dir_scan_interval`` for directory walks) with a single sampler instance:

* ``collect`` — fast path (60 s default).  Calls ``psutil.disk_partitions``
  and ``psutil.disk_usage`` for every *real* filesystem (pseudo-fs excluded).
  Produces ``disk_samples`` rows.

* ``collect_dirs`` — slow path (1 h default).  Walks each ``WatchDir`` in
  ``config.sampling.watch_dirs`` with ``os.scandir`` and a monotonic time
  budget.  If the budget is exhausted before the walk completes, the
  accumulated size is stored with ``is_estimate=1``.  Symbolic links are
  never followed.  ``PermissionError`` on any entry is silenced and
  traversal continues.  Produces ``dir_samples`` rows.

The sampler is registered under the name ``"disk"`` so the recorder can look
it up via ``get_sampler("disk")``.
"""

from __future__ import annotations

import os
import time

import psutil

from ..core.sampler import RunContext, SampleBatch, Sampler, register_sampler
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

    def collect(self, ctx: RunContext) -> list[SampleBatch]:
        """Collect disk_usage for all non-pseudo-fs mount points."""
        ts = time.time()
        rows: list[tuple] = []

        try:
            partitions = psutil.disk_partitions(all=False)
        except Exception:
            partitions = []

        for part in partitions:
            if part.fstype.lower() in _PSEUDO_FS_TYPES:
                continue
            # Some virtual mounts report an empty fstype — skip those too.
            if not part.fstype:
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except OSError:  # incl. PermissionError
                continue
            rows.append(
                make_row(
                    "disk_samples",
                    ts=ts,
                    mountpoint=part.mountpoint,
                    total=usage.total,
                    used=usage.used,
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
        for wd in watch_dirs:
            size_bytes, is_estimate = _scan_dir(wd.path, wd.time_budget_sec)
            rows.append(
                make_row(
                    "dir_samples",
                    ts=ts,
                    path=wd.path,
                    size_bytes=size_bytes,
                    is_estimate=int(is_estimate),
                )
            )

        return [SampleBatch(table="dir_samples", rows=rows)]


def _scan_dir(root: str, time_budget_sec: float) -> tuple[int, bool]:
    """Recursively sum file sizes under *root* within a time budget.

    Returns ``(total_bytes, is_estimate)`` where ``is_estimate`` is ``True``
    when the walk was aborted before completion.  Symbolic links are not
    followed.  ``PermissionError`` on any individual entry is silenced.
    """
    deadline = time.monotonic() + time_budget_sec
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
                    if entry.is_symlink():
                        # Count the symlink itself but do not follow it.
                        total += stat.st_size
                    elif entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    else:
                        total += stat.st_size
        except OSError:  # incl. PermissionError
            continue

    return total, is_estimate
