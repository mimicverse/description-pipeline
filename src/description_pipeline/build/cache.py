"""One content-addressed generation cache; qualification always runs independently."""

import errno
import os
import shutil
import tempfile
from pathlib import Path

from ..io import PipelineError, inventory, read_data, write_json

ENTRIES = ("model", "urdf", "mjcf", "meshes", "config/consumer.json")


def _copy(source: Path, target: Path) -> None:
    for relative in ENTRIES:
        origin, destination = source / relative, target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if origin.is_dir():
            shutil.copytree(origin, destination)
        else:
            shutil.copyfile(origin, destination)


def restore(cache: Path, key: str, staging: Path) -> bool:
    entry = cache / key
    if not entry.exists():
        return False
    manifest = read_data(entry / "cache.json")
    if manifest != {"key": key, "files": inventory(entry, exclude=("cache.json",))}:
        raise PipelineError("Generation cache was modified; remove the affected cache entry and rebuild")
    _copy(entry, staging)
    return True


def save(cache: Path, key: str, staging: Path) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".cache-", dir=cache))
    try:
        _copy(staging, temporary)
        write_json(temporary / "cache.json", {"key": key, "files": inventory(temporary)})
        # A concurrent equivalent build may win; the next reader still verifies it.
        try:
            os.rename(temporary, cache / key)
        except OSError as error:
            # POSIX reports ENOTEMPTY for an existing directory; Windows uses EEXIST.
            if error.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
