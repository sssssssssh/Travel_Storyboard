"""Report names used but not bound inside a function (stranded locals).

Used during the TSB-262 refactor: splitting a function body in two is exactly
the change that silently strands locals, and CPython only reveals it when that
line happens to execute. This finds them statically.
"""
from __future__ import annotations

import ast
import builtins
import sys


def free_names(path: str, func: str) -> list[str]:
    tree = ast.parse(open(path, encoding="utf-8").read())

    # Only *true* module-level bindings. Function parameters must NOT be counted,
    # or a local stranded out of a sibling function looks resolvable and the
    # check silently passes — which is precisely the bug it exists to catch.
    module_globals: set[str] = set(dir(builtins))
    for node in tree.body:
        for sub in ast.walk(node) if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else [node]:
            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                for alias in sub.names:
                    module_globals.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                module_globals.add(sub.name)
            elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                module_globals.add(sub.id)
            elif isinstance(sub, ast.ExceptHandler) and sub.name:
                module_globals.add(sub.name)
            elif isinstance(sub, ast.arg):
                module_globals.add(sub.arg)
            elif isinstance(sub, (ast.For, ast.comprehension)):
                for t in ast.walk(getattr(sub, "target", sub)):
                    if isinstance(t, ast.Name):
                        module_globals.add(t.id)
        if isinstance(node, ast.Assign):
            for t in node.targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Name):
                        module_globals.add(sub.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            module_globals.add(node.target.id)
        elif isinstance(node, ast.Try):
            for sub in ast.walk(node):
                if isinstance(sub, ast.ExceptHandler) and sub.name:
                    module_globals.add(sub.name)
                elif isinstance(sub, ast.ImportFrom):
                    for alias in sub.names:
                        module_globals.add((alias.asname or alias.name).split(".")[0])

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != func:
            continue
        bound = {a.arg for a in node.args.args + node.args.kwonlyargs}
        if node.args.vararg:
            bound.add(node.args.vararg.arg)
        if node.args.kwarg:
            bound.add(node.args.kwarg.arg)
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name) and isinstance(inner.ctx, (ast.Store, ast.Del)):
                bound.add(inner.id)
            elif isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(inner.name)
            elif isinstance(inner, ast.ExceptHandler) and inner.name:
                bound.add(inner.name)
            elif isinstance(inner, (ast.Import, ast.ImportFrom)):
                for alias in inner.names:
                    bound.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(inner, ast.comprehension):
                for t in ast.walk(inner.target):
                    if isinstance(t, ast.Name):
                        bound.add(t.id)
        used = {n.id for n in ast.walk(node)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        return sorted(used - bound - module_globals)
    return [f"<function {func} not found>"]


if __name__ == "__main__":
    path, func = sys.argv[1], sys.argv[2]
    missing = free_names(path, func)
    if missing:
        print(f"STRANDED in {func}: {missing}")
        raise SystemExit(1)
    print(f"clean: {func}")
