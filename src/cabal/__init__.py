"""Cabal — self-hosted personal agent orchestration platform."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tenishai-cabal")
except PackageNotFoundError:  # editable install before metadata is generated
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
