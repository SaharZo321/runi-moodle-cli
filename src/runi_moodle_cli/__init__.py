"""Lightweight cross-platform CLI for Reichman University (RUNI) Moodle."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("runi-moodle-cli")
except PackageNotFoundError:
    __version__ = "1.0.0"
