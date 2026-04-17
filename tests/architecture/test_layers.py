"""Layer boundary enforcement via AST walking.

Ensures import rules between schema/, compile/, and runtime/
are respected at CI time. Zero extra dependencies.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent / "src" / "abe_froman"


def _imports_in_file(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _files_under(subdir: str) -> list[Path]:
    return list((SRC / subdir).rglob("*.py"))


def _starts_with(imports: set[str], prefix: str) -> bool:
    return any(i == prefix or i.startswith(prefix + ".") for i in imports)


class TestSchemaLayerIsolation:
    def test_no_langgraph(self):
        for f in _files_under("schema"):
            imports = _imports_in_file(f)
            assert not _starts_with(imports, "langgraph"), (
                f"{f.relative_to(SRC)} imports langgraph"
            )

    def test_no_compile(self):
        for f in _files_under("schema"):
            imports = _imports_in_file(f)
            assert not _starts_with(imports, "abe_froman.compile"), (
                f"{f.relative_to(SRC)} imports abe_froman.compile"
            )

    def test_no_runtime(self):
        for f in _files_under("schema"):
            imports = _imports_in_file(f)
            assert not _starts_with(imports, "abe_froman.runtime"), (
                f"{f.relative_to(SRC)} imports abe_froman.runtime"
            )


class TestCompileLayerIsolation:
    def test_no_cli(self):
        for f in _files_under("compile"):
            imports = _imports_in_file(f)
            assert not _starts_with(imports, "abe_froman.cli"), (
                f"{f.relative_to(SRC)} imports abe_froman.cli"
            )


class TestRuntimeLayerIsolation:
    def test_no_compile(self):
        for f in _files_under("runtime"):
            imports = _imports_in_file(f)
            assert not _starts_with(imports, "abe_froman.compile"), (
                f"{f.relative_to(SRC)} imports abe_froman.compile"
            )

    def test_no_langgraph(self):
        for f in _files_under("runtime"):
            imports = _imports_in_file(f)
            assert not _starts_with(imports, "langgraph"), (
                f"{f.relative_to(SRC)} imports langgraph"
            )


class TestSchemaTerminology:
    """schema/ files must not use LangGraph-specific identifiers,
    even via aliased imports (e.g. `from langgraph.types import Send as S`)."""

    FORBIDDEN_NAMES = {
        "StateGraph", "Send", "add_node", "add_edge",
        "compiled", "reducer", "checkpointer",
    }

    def test_no_langgraph_identifiers_via_ast(self):
        lower_forbidden = {n.lower() for n in self.FORBIDDEN_NAMES}
        for f in _files_under("schema"):
            tree = ast.parse(f.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id.lower() in lower_forbidden:
                    assert False, (
                        f"{f.relative_to(SRC)}:{node.lineno} uses "
                        f"forbidden identifier '{node.id}'"
                    )
                if isinstance(node, ast.Attribute) and node.attr.lower() in lower_forbidden:
                    assert False, (
                        f"{f.relative_to(SRC)}:{node.lineno} uses "
                        f"forbidden attribute '{node.attr}'"
                    )
