"""PyInstaller entry point.

Pointing PyInstaller at ``src/aries/cli/main.py`` directly makes it treat that
file as a top-level script, which breaks every ``from .. import ...`` inside
the ``aries`` package with::

    ImportError: attempted relative import with no known parent package

This tiny wrapper sits at the repo root with no relative imports of its own;
PyInstaller bundles the whole ``aries`` package as a dependency of *this*
script, and every relative import inside the package resolves correctly.

The console-script entry point declared in ``pyproject.toml`` still points
at ``aries.cli.main:cli`` for `pip install` users — this file is only used
by the PyInstaller workflow.
"""
from aries.cli.main import cli

if __name__ == "__main__":
    cli()
