"""Keep monitor policy independent from effects and its compatibility facade."""
import ast
from pathlib import Path


PACKAGE = Path(__file__).resolve().parents[1] / "cage_core" / "monitoring"


def _dependencies(path):
    """Read both eager and function-local imports when checking direction."""
    internal = set()
    external = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                external.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 1:
                internal.update(
                    [node.module.split(".")[0]] if node.module
                    else [alias.name for alias in node.names]
                )
            else:
                external.add("." * node.level + (node.module or ""))
                if node.module == "cage_core":
                    external.update("cage_core." + alias.name for alias in node.names)
                if node.module is None:
                    external.update("." * node.level + alias.name for alias in node.names)
    return internal, external


def test_accounting_has_no_effectful_dependencies():
    internal, external = _dependencies(PACKAGE / "accounting.py")
    assert internal <= {"constants", "errors", "models", "validation"}
    # These imports support values and calculations only. State, credentials,
    # clocks, subprocesses and transports belong to the orchestration layer.
    assert external <= {
        "__future__", "collections.abc", "dataclasses", "json", "math",
        "types", "typing",
    }


def test_monitor_components_form_an_acyclic_graph_without_facade_imports():
    graph = {}
    for path in PACKAGE.glob("*.py"):
        internal, external = _dependencies(path)
        assert not ({"..monitor", "cage_core.monitor"} & external), path.name
        graph[path.stem] = internal

    visited = set()

    def visit(name, ancestors):
        assert name not in ancestors, " -> ".join((*ancestors, name))
        if name in visited:
            return
        for dependency in graph[name]:
            assert dependency in graph, (name, dependency)
            visit(dependency, (*ancestors, name))
        visited.add(name)

    for name in graph:
        visit(name, ())
