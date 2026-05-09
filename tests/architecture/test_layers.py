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
    # The runtime modules compile/ is allowed to import. compile/ sits
    # above runtime/ in the dependency direction (compile → runtime),
    # but only narrow shared-shape pieces — not orchestration objects
    # like ForemanExecutor, Runner, or auto-detect machinery. Adding a
    # new compile→runtime import means adding it here AND justifying
    # why the imported thing is shared shape rather than orchestration.
    ALLOWED_RUNTIME_MODULES = frozenset({
        "abe_froman.runtime.executor.prompt",  # render_template (pure)
        "abe_froman.runtime.gates",            # eval execution + preamble
        "abe_froman.runtime.logging",          # SubgraphLogger (subgraph wrap)
        "abe_froman.runtime.result",           # ExecutionResult, NodeExecutor
        "abe_froman.runtime.settings_merge",   # merge_settings (pure)
        "abe_froman.runtime.state",            # WorkflowState, REDUCERS
    })

    def test_no_cli(self):
        for f in _files_under("compile"):
            imports = _imports_in_file(f)
            assert not _starts_with(imports, "abe_froman.cli"), (
                f"{f.relative_to(SRC)} imports abe_froman.cli"
            )

    def test_route_is_langgraph_free(self):
        """compile/route.py is a pure state-shape utility — no langgraph
        imports. Mirrors runtime/'s langgraph-free rule."""
        f = SRC / "compile" / "route.py"
        imports = _imports_in_file(f)
        assert not _starts_with(imports, "langgraph"), (
            f"{f.relative_to(SRC)} imports langgraph"
        )

    def test_only_imports_allowed_runtime_modules(self):
        """compile/ may only import the narrow runtime shape modules
        listed in ``ALLOWED_RUNTIME_MODULES``. Importing orchestration
        modules (``runtime.foreman``, ``runtime.runner``,
        ``runtime.executor.dispatch``, ``runtime.executor.backends.*``,
        ``runtime.secrets``, ``runtime.url``) inverts the layer:
        compile would depend on the runtime's wiring rather than its
        shared shape. Add to ``ALLOWED_RUNTIME_MODULES`` only when the
        new import is justified as shared-shape, not orchestration."""
        violations: list[tuple[str, str]] = []
        for f in _files_under("compile"):
            for imp in _imports_in_file(f):
                if not _starts_with({imp}, "abe_froman.runtime"):
                    continue
                if imp in self.ALLOWED_RUNTIME_MODULES:
                    continue
                violations.append((str(f.relative_to(SRC)), imp))
        assert not violations, (
            f"compile/ imports forbidden runtime modules: {violations}. "
            f"Add to ALLOWED_RUNTIME_MODULES with justification, or "
            f"reshape so compile only imports shared shape."
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

    def test_url_module_is_langgraph_free(self):
        """runtime/url.py must stay langgraph-free so schema and compile
        can import it across layer boundaries (covered by the broader
        runtime rule above; pinned here to call out the intent)."""
        f = SRC / "runtime" / "url.py"
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
