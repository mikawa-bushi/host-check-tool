"""Tell whether a path lies on a disk that ``/etc/fstab`` lists but is not mounted."""

from __future__ import annotations

import os
import re
from typing import Optional, Union

FSTAB = "/etc/fstab"
MOUNTINFO = "/proc/self/mountinfo"

_OCTAL = re.compile(r"\\([0-7]{3})")


def _mount_points(path: str, column: int) -> Optional[set[str]]:
    """The absolute paths in *column* of the table at *path*, or None if unreadable.

    Both tables write a space in a path as ``\\040`` (fstab(5), proc(5)).  The
    paths are compared as text and never looked up: a look-up of the mount
    point of a network or FUSE file system whose server stops answering blocks.
    """
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    points = set()
    for line in lines:
        fields = line.split()
        if len(fields) <= column or fields[0].startswith("#"):
            continue
        point = _OCTAL.sub(lambda m: chr(int(m.group(1), 8)), fields[column])
        if point.startswith("/"):
            points.add("/" + os.path.normpath(point).lstrip("/"))
    return points


def unmounted_disk(db_path: Union[str, os.PathLike]) -> Optional[str]:
    """The ``/etc/fstab`` mount point above *db_path* that is not mounted, if any.

    Until a disk attached late (a USB stick, an SD card with ``nofail``) is
    mounted, its mount point is a plain folder on the system disk.  A recorder
    that may write there (root, or a mount point the user owns) would fill the
    system disk with a DB that the mount then hides.  None when nothing is
    listed above *db_path*, or when the tables cannot be read.  Only *db_path*
    is resolved (symlinks), so an fstab line that names its mount point through
    a symlink is not recognised and recording goes on as before.
    """
    listed = _mount_points(FSTAB, 1)
    mounted = _mount_points(MOUNTINFO, 4)
    if not listed or not mounted:
        return None
    folder = os.path.dirname(os.path.realpath(db_path))
    while True:
        if folder in listed and folder not in mounted:
            return folder
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        folder = parent
