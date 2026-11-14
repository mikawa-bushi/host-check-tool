"""Which block devices carry I/O that the devices below them already counted."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Container, Iterable


def is_partition(name: str, names: Container[str]) -> bool:
    """Whether *name* is a partition of one of the disks in *names*.

    The kernel names a partition after its disk and its number, with a ``p``
    between them when the disk's name ends in a digit (``sda1``, ``nvme0n1p1``).
    A disk's name can end in ``p`` itself (``sdp``, ``xvdp``), so ``sdp1`` is
    read as that disk's first partition before it is read as ``sd`` plus ``p1``.
    A disk's counters already include its partitions.
    """
    stem = name.rstrip("0123456789")
    if not stem or stem == name:
        return False
    if stem in names:
        return True
    disk = stem[:-1]
    return stem.endswith("p") and disk[-1:].isdigit() and disk in names


def _is_loop(dev_dir: Path) -> bool:
    try:
        return (dev_dir / "loop").is_dir()
    except OSError:
        return False


def _slaves(dev_dir: Path) -> list[str]:
    """Names of the devices the one at *dev_dir* is built on (LVM, dm-crypt, md)."""
    try:
        return [p.name for p in (dev_dir / "slaves").iterdir()]
    except OSError:
        return []


def is_stacked(dev_dir: Path) -> bool:
    """Whether the block device at *dev_dir* sits on other devices or on a file.

    That is LVM, dm-crypt and md (``slaves/`` lists what they are built on) and
    loop devices.  Their I/O is counted again by the device below, so a total
    over every device would count it twice.  A device whose sysfs entry cannot
    be read counts as not stacked.
    """
    return _is_loop(dev_dir) or bool(_slaves(dev_dir))


def _name(by_number_link: Path) -> str:
    """Device name behind ``/sys/dev/block/<major:minor>`` ('' if unreadable)."""
    try:
        return Path(os.readlink(by_number_link)).name
    except OSError:
        return ""


def counted_devices(listed: Iterable[str], sys_root: Path) -> set[str]:
    """The ``major:minor`` devices of one cgroup io.stat whose I/O is not listed again.

    A stacked device is left out only when the device it sits on is in the same
    io.stat: one of its slaves (or the disk a slave is a partition of), or, for a
    loop device, any other device that is not a loop.  Otherwise leaving it out
    would drop that I/O altogether.
    """
    by_number = sys_root / "dev" / "block"
    names = {dev: _name(by_number / dev) for dev in listed}
    present = set(names.values())
    left_out = set()
    for dev in names:
        if _is_loop(by_number / dev):
            covered = any(
                not _is_loop(by_number / other) for other in names if other != dev
            )
        else:
            covered = any(
                s in present or is_partition(s, present)
                for s in _slaves(by_number / dev)
            )
        if covered:
            left_out.add(dev)
    return set(names) - left_out
