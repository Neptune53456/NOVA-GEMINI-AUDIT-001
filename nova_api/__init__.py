"""Local read-only API for Nova."""

from .app import create_app

__all__ = ["create_app"]
