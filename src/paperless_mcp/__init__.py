"""Safety-focused Paperless-ngx access through MCP and a CLI."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("paperless-mcp")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0.0.0+local"

__all__ = ["__version__"]
