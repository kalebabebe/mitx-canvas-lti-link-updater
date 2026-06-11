"""
Content-based detection of course export files.

File names and extensions are unreliable (users rename files, browsers
mangle extensions), so both the CLI and the web app identify uploads by
looking inside them:

- Canvas export: a ZIP archive containing imsmanifest.xml
- edX OLX export: a gzipped tar containing course.xml
"""

import tarfile
import zipfile
from pathlib import Path


def is_canvas_export(path) -> bool:
    """A Canvas export is a ZIP archive containing imsmanifest.xml."""
    path = Path(path)
    if not zipfile.is_zipfile(path):
        return False
    try:
        with zipfile.ZipFile(path) as zf:
            return any(Path(n).name == 'imsmanifest.xml' for n in zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return False


def is_edx_export(path) -> bool:
    """An edX OLX export is a gzipped tar containing course.xml."""
    path = Path(path)
    try:
        with tarfile.open(path, 'r:gz') as tf:
            return any(Path(n).name == 'course.xml' for n in tf.getnames())
    except (tarfile.TarError, OSError, EOFError):
        return False


def identify(path) -> str:
    """Return 'canvas', 'edx', or 'unknown' for a file path."""
    if is_canvas_export(path):
        return 'canvas'
    if is_edx_export(path):
        return 'edx'
    return 'unknown'
