"""Write an output file so that it is complete or not there at all."""

from __future__ import annotations

import errno
import os
import shutil
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import IO, Any, Iterator, Union


def read_only_filesystem(path: Union[str, Path]) -> bool:
    """Whether *path* is on a file system mounted read-only.

    ``os.access`` is False both for a missing permission and for such a mount,
    and only the first can be cured with ``chmod``.
    """
    try:
        return bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except OSError:
        return False


@contextmanager
def atomic_output(
    path: Union[str, Path], mode: str = "w", **kwargs: Any
) -> Iterator[IO]:
    """Open *path* for writing; it is replaced only when the block succeeds.

    The data goes to a temporary file beside *path*.  A write that fails (the
    disk is full) or an error in the block removes that file, so no partial
    report is left behind and a file already at *path* stays as it was.  The
    data is on disk before it takes the old file's place, so a power cut does
    not leave an empty report.  A device or pipe (``/dev/stdout``), a symlink,
    a file with other hard links, or a file in a directory the user cannot
    write cannot be replaced without cutting those links, so it is opened as is.
    """
    target = Path(path)
    if target.exists() and (
        not target.is_file()
        or target.is_symlink()
        or target.stat().st_nlink > 1
        or not os.access(target.parent, os.W_OK | os.X_OK)
    ):
        with open(target, mode, **kwargs) as fh:
            yield fh
        return
    # A prefix of the name keeps the temporary name within the file name
    # limit when *path* itself is close to it.
    temp = target.with_name(f".{target.name[:64]}.{os.getpid()}.part")
    try:
        with open(temp, mode, **kwargs) as fh:
            if target.exists():
                shutil.copymode(target, temp)
            yield fh
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError as exc:
                if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                    raise
        os.replace(temp, target)
    except BaseException:
        with suppress(OSError):
            temp.unlink()
        raise
