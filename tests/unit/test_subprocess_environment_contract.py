"""Prevent partial PyInstaller child-environment fixes across production code."""

import ast
import os
import sys
import unittest
from pathlib import Path
from typing import Dict, Optional
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOTS = (ROOT / "common", ROOT / "service", ROOT / "tools")
SUBPROCESS_CALLS = {"run", "Popen", "call", "check_call", "check_output"}
WRAPPERS = {
    Path("common/utils.py"): {"run_command": "_system_subprocess_env"},
    Path("tools/kafka/KafkaCli.py"): {"run_command": "_env_for_system_subprocess"},
    Path("tools/starrocks/StarCli.py"): {"run_command": "_env_for_system_subprocess"},
    Path("tools/k8stools/KubePublishCli.py"): {"run": "_env_for_system_subprocess"},
}


class _SubprocessCallVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.functions = []
        self.offenders = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.functions.append(node)
        self.generic_visit(node)
        self.functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        is_subprocess_call = (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
            and func.attr in SUBPROCESS_CALLS
        )
        if is_subprocess_call and not any(kw.arg == "env" for kw in node.keywords):
            enclosing = self.functions[-1] if self.functions else None
            function = enclosing.name if enclosing is not None else "<module>"
            relative = self.path.relative_to(ROOT)
            expected_helper = WRAPPERS.get(relative, {}).get(function)
            if expected_helper is None:
                self.offenders.append(f"{relative}:{node.lineno} ({function})")
            else:
                helper_calls = {
                    call.func.id
                    for call in ast.walk(enclosing)
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                }
                if expected_helper not in helper_calls:
                    self.offenders.append(
                        f"{relative}:{node.lineno} ({function} missing {expected_helper})"
                    )
        self.generic_visit(node)


class TestSubprocessEnvironmentContract(unittest.TestCase):
    def test_every_external_process_uses_the_frozen_environment_boundary(self):
        offenders = []
        for production_root in PRODUCTION_ROOTS:
            for path in production_root.rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                visitor = _SubprocessCallVisitor(path)
                visitor.visit(tree)
                offenders.extend(visitor.offenders)
        self.assertEqual([], offenders)

    def test_each_independent_tool_with_subprocesses_owns_its_environment_helper(self):
        offenders = []
        for path in (ROOT / "tools").rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if "subprocess." not in source:
                continue
            tree = ast.parse(source, filename=str(path))
            helper_names = {
                node.name for node in tree.body if isinstance(node, ast.FunctionDef)
            }
            if "_env_for_system_subprocess" not in helper_names:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual([], offenders)

    def test_each_independent_tool_helper_restores_or_removes_bundle_path(self):
        for path in (ROOT / "tools").rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if "subprocess." not in source:
                continue
            tree = ast.parse(source, filename=str(path))
            helper = next(
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef)
                and node.name == "_env_for_system_subprocess"
            )
            namespace = {
                "os": os,
                "sys": sys,
                "Dict": Dict,
                "Optional": Optional,
            }
            exec(compile(ast.Module(body=[helper], type_ignores=[]), str(path), "exec"), namespace)
            function = namespace["_env_for_system_subprocess"]

            with self.subTest(tool=str(path.relative_to(ROOT)), case="restore"):
                with (
                    patch.object(sys, "frozen", True, create=True),
                    patch.object(sys, "platform", "linux"),
                    patch.dict(
                        os.environ,
                        {
                            "LD_LIBRARY_PATH": "/tmp/_MEI-test",
                            "LD_LIBRARY_PATH_ORIG": "/host/lib",
                        },
                        clear=True,
                    ),
                ):
                    self.assertEqual(function()["LD_LIBRARY_PATH"], "/host/lib")

            with self.subTest(tool=str(path.relative_to(ROOT)), case="remove"):
                with (
                    patch.object(sys, "frozen", True, create=True),
                    patch.object(sys, "platform", "linux"),
                    patch.dict(os.environ, {"LD_LIBRARY_PATH": "/tmp/_MEI-test"}, clear=True),
                ):
                    self.assertNotIn("LD_LIBRARY_PATH", function())


if __name__ == "__main__":
    unittest.main()
