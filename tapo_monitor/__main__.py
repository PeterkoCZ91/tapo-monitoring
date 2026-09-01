"""Run the package with ``python -m tapo_monitor`` — the spelling a release unit uses.

The ``tapo-monitor`` console script exists only where pip generated it. A release
directory is an extracted archive, not an installed package, so the systemd unit starts
``<venv-python> -m tapo_monitor run ...`` from inside ``~/tapo-monitor/current`` and this
module hands straight to the one CLI ``main`` — a second argv contract here would drift.

The ``__name__`` guard is load-bearing: ``selfcheck`` imports every module in the
package (that is how it catches a half-copied tree), and without the guard that import
would start the daemon inside the very check that gates a deploy.
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
