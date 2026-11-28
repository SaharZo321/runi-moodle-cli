"""Lightweight cross-platform CLI for Moodle."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("moodle-cli")
except PackageNotFoundError:
    __version__ = "1.2.0"
