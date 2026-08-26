"""Import hygiene for the package as a whole.

The fleet does not run the same third-party packages: the host with the recorder has
neither numpy nor onnxruntime nor Pillow nor OpenCV, one Pi has numpy and OpenCV but no
onnxruntime, and only the scorer host has the full inference stack. That works because
every third-party import in this package is inside a function, so a module a site never
calls costs that site nothing. It is a load-bearing property, not an accident, and the
first module-level `import numpy` would take down the site that lacks it at startup -
in the same deploy that passed CI everywhere.
"""

import ast
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tapo_monitor

PACKAGE_DIR = pathlib.Path(tapo_monitor.__file__).parent


def _module_scope_imports(tree):
    """Top-level import names only; guarded and function-local imports are fine."""
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            yield (node.module or "").split(".")[0]


def test_package_modules_import_without_any_third_party_dependency():
    offenders = []
    modules = sorted(PACKAGE_DIR.glob("*.py"))
    assert modules, "no package modules found"
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name in _module_scope_imports(tree):
            if name and name != "tapo_monitor" and name not in sys.stdlib_module_names:
                offenders.append(f"{path.name}: import {name}")
    assert offenders == []
