"""Keep the package importable on older Pythons than the test suite runs on.

`scripts/jev_live_check.py` is meant to run on whatever Python someone already
has — macOS ships 3.9 with the Command Line Tools — so the modules it imports
must not evaluate a PEP 604 union (`X | None`) at run time. That syntax only
became valid at run time in 3.10; before then it raises

    TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'

`from __future__ import annotations` defers every annotation to a string and
makes the whole question moot, so the rule is simply that every module has it.
A missing one in `backends/__init__.py` is exactly how this broke once.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCES = sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "scripts").rglob("*.py"))


def has_future_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


# Builtin names that only appear either side of a `|` when a *type* union is
# meant. Bitwise OR of flags (`os.O_RDWR | os.O_CREAT`) is ordinary runtime
# code and must not be flagged, so the check looks at what is being combined.
TYPE_NAMES = frozenset(
    "str int float bool bytes dict list set tuple frozenset complex object type".split()
)


def looks_like_a_type(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant) and node.value is None:
        return True  # `X | None`, the form that actually broke
    if isinstance(node, ast.Name):
        return node.id in TYPE_NAMES or node.id[:1].isupper()
    if isinstance(node, ast.Subscript):
        return looks_like_a_type(node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return looks_like_a_type(node.left) or looks_like_a_type(node.right)
    return False


def runtime_unions(tree: ast.Module) -> list[int]:
    """Line numbers of a *type* union outside an annotation, which 3.9 cannot evaluate."""
    found: list[int] = []

    class Visitor(ast.NodeVisitor):
        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            # The annotation itself is deferred; the value is not.
            if node.value:
                self.visit(node.value)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._function(node)

        def _function(self, node) -> None:
            for default in node.args.defaults + [d for d in node.args.kw_defaults if d]:
                self.visit(default)
            for statement in node.body:
                self.visit(statement)
            for decorator in node.decorator_list:
                self.visit(decorator)

        def visit_BinOp(self, node: ast.BinOp) -> None:
            if isinstance(node.op, ast.BitOr) and (
                looks_like_a_type(node.left) or looks_like_a_type(node.right)
            ):
                found.append(node.lineno)
            self.generic_visit(node)

    Visitor().visit(tree)
    return found


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_module_defers_its_annotations(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert has_future_annotations(tree), (
        f"{path.relative_to(ROOT)} is missing `from __future__ import annotations`, "
        "so its annotations are evaluated at run time and it will fail on Python 3.9"
    )


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_union_is_evaluated_at_runtime(path: Path):
    lines = runtime_unions(ast.parse(path.read_text(encoding="utf-8")))
    assert not lines, (
        f"{path.relative_to(ROOT)} evaluates `|` outside an annotation at "
        f"line(s) {lines}; on Python 3.9 a type union there raises TypeError"
    )


def test_the_live_check_script_states_its_floor():
    """The script guards its own version rather than failing with a TypeError."""
    text = (ROOT / "scripts" / "jev_live_check.py").read_text(encoding="utf-8")
    assert "sys.version_info < (3, 9)" in text
