"""Which block devices carry I/O that the devices below them already counted."""

from __future__ import annotations

from pathlib import Path


def is_stacked(dev_dir: Path) -> bool:
    """Whether the block device at *dev_dir* sits on other devices or on a file.

    That is LVM, dm-crypt and md (``slaves/`` lists what they are built on) and
    loop devices.  Their I/O is counted again by the device below, so a total
    over every device would count it twice.  A device whose sysfs entry cannot
    be read counts as not stacked.
    """
    try:
        return (dev_dir / "loop").is_dir() or any((dev_dir / "slaves").iterdir())
    except OSError:
        return False
