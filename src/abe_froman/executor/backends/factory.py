from __future__ import annotations

from abe_froman.executor.prompt_backend import PromptBackend


def create_prompt_backend(executor_type: str, **kwargs: object) -> PromptBackend:
    """Create a PromptBackend instance from a type identifier.

    Supported types:
    - "stub": placeholder backend (default, no external dependencies)
    - "acp": ACP via claude-code-acp adapter
    """
    if executor_type == "stub":
        from abe_froman.executor.backends.stub import StubBackend

        return StubBackend()

    if executor_type == "acp":
        from abe_froman.executor.backends.acp import ACPBackend

        return ACPBackend(
            program=kwargs.get("program", "npx"),
            args=kwargs.get("args", ("@zed-industries/claude-code-acp",)),
        )

    raise ValueError(
        f"Unknown executor type: {executor_type!r}. Supported: stub, acp"
    )
