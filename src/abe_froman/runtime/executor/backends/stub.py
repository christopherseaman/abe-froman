from __future__ import annotations

from abe_froman.runtime.result import ExecutionResult


class StubBackend:
    """Returns a placeholder response. Used when no real backend is configured."""

    async def send_prompt(
        self, prompt: str, model: str, workdir: str
    ) -> ExecutionResult:
        return ExecutionResult(
            output=f"[prompt-stub] model={model} prompt_length={len(prompt)}"
        )

    async def close(self) -> None:
        pass
