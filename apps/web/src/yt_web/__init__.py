"""Lightweight gateway application API; HTTP runtime is available in yt_web.http."""

from .application import WebApplication, create_app

__all__ = ["WebApplication", "create_app"]
