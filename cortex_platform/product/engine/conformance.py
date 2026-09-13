"""AST conformance scan over the ambient inputs the research engine reads.

D3's binding table is only total if something other than prose owns the list.
Two independent greps of the shipped engine disagreed by ten names, so the list
is derived here from the syntax tree and asserted against `ENGINE_BINDINGS` by a
unit test: a new `os.environ` read in the engine that nobody dispositioned turns
the suite red instead of silently inheriting the daemon's environment.

The scan's scope is frozen by the contract, not chosen here: literal reads, one
hop of helper propagation through the six named helpers, and the
`cortex_platform` modules the engine imports. Anything it cannot resolve
statically is enumerated with a reason in `bindings.py` rather than wildcarded.
"""

from __future__ import annotations

import ast
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

# Any function whose body reads the environment under one of its own parameters
# propagates a literal call site one hop, and the scan follows all of them. The
# contract froze six such helpers; five of them (`_env_int`, `_env_float`,
# `_budget`, `_max_age`, `_server_env`) lived in modules the supported surface no
# longer ships and left with them. `_env_cred` is the one that still exists, and
# it is the important one: it carries all three OCR credentials, which a literal
# scan alone would miss entirely.
PROPAGATING_HELPERS: frozenset[str] = frozenset({"_env_cred"})

# The `cortex_platform` modules the engine imports. There are none: the nine
# supported `cortex_research` modules import no platform code at all, which is
# what makes the legacy singletons removable and is asserted at runtime by
# `tests/product/engine/test_runtime_closure_proof.py`. The mechanism stays so a
# future import is describable rather than discovered at build time.
PLATFORM_MODULES: tuple[str, ...] = ()


@dataclass(frozen=True)
class EnvironmentRead:
    """One resolved ambient read: a variable name and where it is read."""

    name: str
    location: str
    hop: str  # "literal" or the helper the literal travelled through


@dataclass(frozen=True)
class DynamicRead:
    """One ambient read whose variable name is not a literal at the site."""

    location: str
    expression: str


@dataclass(frozen=True)
class ScanResult:
    reads: tuple[EnvironmentRead, ...]
    dynamic: tuple[DynamicRead, ...]
    helpers: Mapping[str, str]

    @property
    def names(self) -> frozenset[str]:
        return frozenset(read.name for read in self.reads)


def engine_package_root() -> Path:
    """Locate the installed `cortex_research` package without importing it."""

    spec = importlib.util.find_spec("cortex_research")
    if spec is None or spec.origin is None:
        raise RuntimeError("cortex_research is not importable from this interpreter")
    return Path(spec.origin).parent


def platform_module_paths(
    modules: Iterable[str] = PLATFORM_MODULES,
) -> tuple[Path, ...]:
    paths: list[Path] = []
    for name in modules:
        spec = importlib.util.find_spec(name)
        if spec is None or spec.origin is None:
            raise RuntimeError(f"{name} is not importable from this interpreter")
        paths.append(Path(spec.origin))
    return tuple(paths)


def _environ_key(node: ast.AST) -> ast.expr | None:
    """Return the expression naming the variable in one ambient read."""

    def _is_environ(value: ast.expr) -> bool:
        if isinstance(value, ast.Attribute) and value.attr == "environ":
            return isinstance(value.value, ast.Name) and value.value.id == "os"
        return isinstance(value, ast.Name) and value.id == "environ"

    if isinstance(node, ast.Subscript) and _is_environ(node.value):
        return node.slice
    if isinstance(node, ast.Call):
        function = node.func
        if (
            isinstance(function, ast.Attribute)
            and function.attr in {"get", "setdefault", "pop"}
            and _is_environ(function.value)
        ):
            return node.args[0] if node.args else None
        if (
            isinstance(function, ast.Attribute)
            and function.attr == "getenv"
            and isinstance(function.value, ast.Name)
            and function.value.id == "os"
        ):
            return node.args[0] if node.args else None
        if isinstance(function, ast.Name) and function.id == "getenv":
            return node.args[0] if node.args else None
    return None


def _helper_key_position(function: ast.FunctionDef | ast.AsyncFunctionDef) -> int | None:
    """Return the positional index whose value this helper reads from the env.

    `-1` marks a `*args` helper (`_server_env(*keys)`), whose every positional
    argument is a variable name.
    """

    positional = [argument.arg for argument in function.args.args]
    vararg = function.args.vararg.arg if function.args.vararg else None
    # `_server_env(*keys)` reads `os.environ[k] for k in keys`, so the loop
    # target has to be resolved back to the parameter it iterates or the helper
    # looks like it reads nothing.
    aliases: dict[str, str] = {}
    for node in ast.walk(function):
        generators = (
            node.generators
            if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp))
            else []
        )
        for generator in generators:
            if isinstance(generator.iter, ast.Name) and isinstance(
                generator.target, ast.Name
            ):
                aliases[generator.target.id] = generator.iter.id
        if (
            isinstance(node, ast.For)
            and isinstance(node.iter, ast.Name)
            and isinstance(node.target, ast.Name)
        ):
            aliases[node.target.id] = node.iter.id
    for node in ast.walk(function):
        key = _environ_key(node)
        if not isinstance(key, ast.Name):
            continue
        name = aliases.get(key.id, key.id)
        if vararg is not None and name == vararg:
            return -1
        if name in positional:
            return positional.index(name)
    return None


def _literal_iterations(tree: ast.AST) -> dict[str, tuple[str, ...]]:
    """Map a loop target onto the literal names it iterates.

    The excluded legacy MCP sandbox server read `os.environ[k] for k in
    ("PATH", ..., "HOME", ...)`: the name is a literal at the site even though
    the subscript is not, and HOME was one of them -- exactly the variable
    Layer 2 may never leave unbound. No retained module writes that shape today,
    so this is the pattern the scan keeps resolving rather than a live site.
    `test_conformance.py` proves it on a synthetic module.
    """

    resolved: dict[str, tuple[str, ...]] = {}

    def _literals(node: ast.expr) -> tuple[str, ...] | None:
        if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return None
        values = []
        for element in node.elts:
            if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
                return None
            values.append(element.value)
        return tuple(values)

    for node in ast.walk(tree):
        targets: list[tuple[ast.expr, ast.expr]] = []
        if isinstance(node, ast.For):
            targets.append((node.target, node.iter))
        elif isinstance(
            node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
        ):
            targets.extend(
                (generator.target, generator.iter) for generator in node.generators
            )
        for target, iterable in targets:
            names = _literals(iterable)
            if names is None or not isinstance(target, ast.Name):
                continue
            resolved[target.id] = resolved.get(target.id, ()) + names
    return resolved


def scan_environment_reads(paths: Iterable[Path]) -> ScanResult:
    """Collect every ambient read the frozen scope can resolve."""

    trees: dict[Path, ast.Module] = {}
    for path in sorted(set(paths)):
        trees[path] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    helper_positions: dict[str, set[int]] = {}
    helper_sites: dict[str, str] = {}
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            position = _helper_key_position(node)
            if position is None:
                continue
            helper_positions.setdefault(node.name, set()).add(position)
            helper_sites.setdefault(node.name, f"{path.name}:{node.lineno}")

    reads: list[EnvironmentRead] = []
    dynamic: list[DynamicRead] = []
    for path, tree in trees.items():
        iterations = _literal_iterations(tree)
        for node in ast.walk(tree):
            key = _environ_key(node)
            if key is not None:
                location = f"{path.name}:{node.lineno}"
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    reads.append(EnvironmentRead(key.value, location, "literal"))
                elif isinstance(key, ast.Name) and key.id in iterations:
                    for name in iterations[key.id]:
                        reads.append(EnvironmentRead(name, location, "literal_loop"))
                else:
                    dynamic.append(DynamicRead(location, ast.unparse(key)))
                continue
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = (
                function.id
                if isinstance(function, ast.Name)
                else function.attr
                if isinstance(function, ast.Attribute)
                else None
            )
            positions = helper_positions.get(name) if name else None
            if not positions:
                continue
            for position in sorted(positions):
                candidates = (
                    node.args
                    if position == -1
                    else node.args[position : position + 1]
                )
                for argument in candidates:
                    if isinstance(argument, ast.Constant) and isinstance(
                        argument.value, str
                    ):
                        reads.append(
                            EnvironmentRead(
                                argument.value, f"{path.name}:{node.lineno}", name
                            )
                        )
    return ScanResult(
        reads=tuple(reads), dynamic=tuple(dynamic), helpers=dict(helper_sites)
    )


def scan_engine_surface() -> ScanResult:
    """Scan the whole frozen surface: the engine package plus its platform imports."""

    paths = list(engine_package_root().rglob("*.py"))
    paths.extend(platform_module_paths())
    return scan_environment_reads(paths)
