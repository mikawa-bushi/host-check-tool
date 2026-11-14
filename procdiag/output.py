"""Write an output file so that it is complete or not there at all."""

from __future__ import annotations

import os
import shutil
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import IO, Any, Iterator, Union


@contextmanager
def atomic_output(
    path: Union[str, Path], mode: str = "w", **kwargs: Any
) -> Iterator[IO]:
    """Open *path* for writing; it is replaced only when the block succeeds.

    The data goes to a temporary file beside *path*.  A write that fails (the
    disk is full) or an error in the block removes that file, so no partial
    report is left behind and a file already at *path* stays as it was.  A
    device or pipe (``/dev/stdout``), a symlink, or a file in a directory the
    user cannot write cannot be replaced, so it is opened as is.
    """
    target = Path(path)
    if target.exists() and (
        not target.is_file()
        or target.is_symlink()
        or not os.access(target.parent, os.W_OK | os.X_OK)
    ):
        with open(target, mode, **kwargs) as fh:
            yield fh
        return
    temp = target.with_name(f".{target.name}.{os.getpid()}.part")
    try:
        with open(temp, mode, **kwargs) as fh:
            if target.exists():
                shutil.copymode(target, temp)
            yield fh
        os.replace(temp, target)
    except BaseException:
        with suppress(OSError):
            temp.unlink()
        raise
