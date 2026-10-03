"""Static checks over the source tree.

These catch a class of bug that normal execution hides: because every module
uses ``from __future__ import annotations``, annotations are strings and are
never evaluated. A name used in an annotation but never imported therefore
looks fine at import time and at runtime, and only fails much later, in
something that resolves type hints (``typing.get_type_hints``, pydantic model
rebuilds, a documentation tool). ``Settings.root_dir`` shipped with exactly
that bug -- ``Optional`` was used in its return type and never imported.
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

import pytest

CIRCLE_ROOT = Path(__file__).resolve().parent.parent / "circle"


def _module_files() -> list[Path]:
    return sorted(p for p in CIRCLE_ROOT.rglob("*.py"))


def _names_in_scope(tree: ast.Module) -> set[str]:
    """Every name a module could legally reference in an annotation."""
    names = set(dir(builtins))

    class Collect(ast.NodeVisitor):
        def visit_Import(self, node: ast.Import) -> None:
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            for a in node.names:
                names.add(a.asname or a.name)

        def visit_Assign(self, node: ast.Assign) -> None:
            for t in node.targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            names.add(node.name)
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            names.add(node.name)
            self.generic_visit(node)

    Collect().visit(tree)
    return names


def _annotation_names(annotation: ast.expr) -> set[str]:
    # Attribute access (a.b) and calls are resolved, not name-bound; only bare
    # names need to be in scope.
    return {
        n.id
        for n in ast.walk(annotation)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }


@pytest.mark.parametrize("path", _module_files(), ids=lambda p: p.name)
def test_annotations_reference_only_names_in_scope(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf8"), str(path))
    in_scope = _names_in_scope(tree)

    missing: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        annotations: list[tuple[str, ast.expr]] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                annotations.append((f"{node.name}() return", node.returns))
            args = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            for a in args:
                if a.annotation is not None:
                    annotations.append((f"{node.name}({a.arg})", a.annotation))
            for a in (node.args.vararg, node.args.kwarg):
                if a is not None and a.annotation is not None:
                    annotations.append((f"{node.name}(*{a.arg})", a.annotation))
        elif isinstance(node, ast.AnnAssign):
            annotations.append(("variable", node.annotation))

        for where, ann in annotations:
            for name in _annotation_names(ann) - in_scope:
                missing.add((where, name))

    assert not missing, (
        f"{path.name} uses names in annotations that it never defines or "
        f"imports: {sorted(missing)}. This passes at import time only because "
        f"annotations are lazy; it will break anything that resolves them."
    )


def test_settings_root_dir_annotations_resolve() -> None:
    """The concrete regression, kept as a readable guard."""
    import typing

    from circle.config import Settings

    typing.get_type_hints(Settings.root_dir)