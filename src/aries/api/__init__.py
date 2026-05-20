"""HTTP API + static asset server for the web dashboard (Phase 5).

`DashboardAPI` runs alongside the Aries daemon and exposes seven read-only
JSON endpoints plus a Server-Sent Events stream for live activity. The same
aiohttp app serves the React frontend build out of ``<repo>/dashboard/dist/``
when present; without a build, JSON endpoints still work standalone.
"""
from .server import DashboardAPI

__all__ = ["DashboardAPI"]
