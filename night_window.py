"""Backward-compatible shim. The implementation now lives in tapo_monitor.scheduling.

Kept so the standalone scripts (camera_automation.py, person_monitor.py, groq_watch.py)
keep working with `from night_window import is_night` during the migration to the package.
"""

from tapo_monitor.scheduling import (  # noqa: F401
    describe_window,
    is_night,
)
