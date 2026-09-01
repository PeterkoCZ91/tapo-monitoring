"""The ``python -m tapo_monitor`` entry point.

The console script only exists where pip generated it; a release directory is an
extracted archive, so the systemd unit starts the package as ``<venv-python> -m
tapo_monitor``. Two properties carry that: the module must dispatch into the one CLI
``main`` (no second argv contract), and importing it must do nothing — ``selfcheck``
imports every module in the package, and an unguarded ``__main__`` would start the
daemon inside the check that gates a deploy.
"""

import os
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _run(args, cwd):
    env = dict(os.environ)
    # The package must resolve through PYTHONPATH, not the cwd: the unit runs from
    # inside a release directory, but the test asserts "from any cwd".
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run([sys.executable, *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=60)


def test_python_dash_m_runs_the_cli_from_any_cwd(tmp_path):
    from tapo_monitor import __version__

    result = _run(["-m", "tapo_monitor", "version"], cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == f"tapo-monitor {__version__}"
    fingerprint = [line for line in lines if line.startswith("package ")][0].split()[1]
    assert len(fingerprint) == 12


def test_importing_the_module_entry_point_runs_nothing(tmp_path):
    # No cameras.yaml exists in tmp_path, so if the import dispatched into main() the
    # default "run" command would fail loudly (or hang) instead of printing the marker.
    result = _run(["-c", "import tapo_monitor.__main__; print('inert')"], cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "inert"


def test_module_entry_point_delegates_to_the_cli_main():
    from tapo_monitor import __main__, cli

    assert __main__.main is cli.main
