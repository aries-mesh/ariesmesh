"""Static assets for the web dashboard (Phase 5 / Phase 5 Addendum).

This sub-package exists so setuptools picks up ``dist/`` as package data —
the built React app is bundled into every wheel and PyInstaller binary so
``aries start`` serves a working dashboard without the end user ever
touching Node or npm.

The path resolution lives in :mod:`aries.api.server` (``_get_dashboard_dir``).
"""
