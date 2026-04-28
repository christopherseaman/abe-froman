from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, Field, model_validator


class PromptExecution(BaseModel):
    type: Literal["prompt"] = "prompt"
    prompt_file: str


class CommandExecution(BaseModel):
    type: Literal["command"] = "command"
    command: str
    args: list[str] = []


class GateOnlyExecution(BaseModel):
    type: Literal["gate_only"] = "gate_only"


Execution = Annotated[
    PromptExecution | CommandExecution | GateOnlyExecution,
    Field(discriminator="type"),
]


def _normalize_prompt_shorthand(instance: Any) -> Any:
    """Convert prompt_file shorthand to PromptExecution.

    Shared by Phase and FinalPhase — both support the prompt_file
    convenience field that auto-converts to execution.type=prompt.
    """
    if instance.prompt_file and instance.execution is None:
        instance.execution = PromptExecution(prompt_file=instance.prompt_file)
        instance.prompt_file = None
    return instance


class DimensionCheck(BaseModel):
    field: str
    min: float = Field(ge=0.0, le=1.0)


class Evaluation(BaseModel):
    """Evaluation configuration for a phase.

    The YAML DSL key stays `quality_gate:` for backward compatibility with
    existing workflows; in Python code the attribute on `Phase` is
    `evaluation`. Unified with the runtime machinery (EvaluationRecord,
    state.evaluations, _make_evaluation_node).
    """
    validator: str
    threshold: float = Field(ge=0.0, le=1.0, default=0.0)
    blocking: bool = False
    max_retries: int | None = None  # None = defer to Settings.max_retries
    model: str | None = None  # override for .md LLM gates; None = Settings.default_model
    dimensions: list[DimensionCheck] | None = None


class OutputContract(BaseModel):
    base_directory: str
    required_files: list[str] = []


class SubphaseTemplate(BaseModel):
    prompt_file: str
    evaluation: Evaluation | None = Field(default=None, alias="quality_gate")

    model_config = {"populate_by_name": True}


class FinalPhase(BaseModel):
    id: str
    name: str
    description: str | None = None
    prompt_file: str | None = None
    execution: Execution | None = None
    evaluation: Evaluation | None = Field(default=None, alias="quality_gate")

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def normalize_prompt_file(self) -> Self:
        return _normalize_prompt_shorthand(self)


class DynamicPhaseConfig(BaseModel):
    enabled: bool = False
    manifest_path: str | None = None
    template: SubphaseTemplate | None = None
    final_phases: list[FinalPhase] = []


class Settings(BaseModel):
    output_directory: str = "output"
    max_retries: int = 3
    default_model: str = "sonnet"
    executor: str = "stub"
    default_timeout: float | None = None  # seconds, None = no timeout
    preamble_file: str | None = None
    retry_backoff: list[float] = []  # delay in seconds per retry attempt
    model_downgrade_chain: list[str] = ["opus", "sonnet", "haiku"]
    max_parallel_jobs: int = 4  # foreman global concurrency cap
    per_model_limits: dict[str, int] = {}  # foreman per-model caps, e.g. {"opus": 2}


class Phase(BaseModel):
    id: str
    name: str
    description: str | None = None
    model: str | None = None
    prompt_file: str | None = None
    execution: Execution | None = None
    depends_on: list[str] = []
    evaluation: Evaluation | None = Field(default=None, alias="quality_gate")
    output_contract: OutputContract | None = None
    dynamic_subphases: DynamicPhaseConfig | None = None
    timeout: float | None = None  # seconds, None = use default

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def normalize_prompt_file(self) -> Self:
        return _normalize_prompt_shorthand(self)

    def effective_timeout(self, settings: Settings) -> float | None:
        """Phase timeout > settings default_timeout. None = no timeout."""
        if self.timeout is not None:
            return self.timeout
        return settings.default_timeout

    def effective_max_retries(self, settings: Settings) -> int:
        """Resolve max_retries: evaluation override > settings default."""
        if self.evaluation and self.evaluation.max_retries is not None:
            return self.evaluation.max_retries
        return settings.max_retries


class WorkflowConfig(BaseModel):
    name: str
    version: str
    phases: list[Phase]
    settings: Settings = Settings()

    @model_validator(mode="after")
    def validate_phase_references(self) -> Self:
        phase_ids = {p.id for p in self.phases}

        if len(phase_ids) != len(self.phases):
            seen = set()
            for p in self.phases:
                if p.id in seen:
                    raise ValueError(f"Duplicate phase id: {p.id}")
                seen.add(p.id)

        for phase in self.phases:
            for dep in phase.depends_on:
                if dep == phase.id:
                    raise ValueError(
                        f"Phase '{phase.id}' has a self-dependency"
                    )
                if dep not in phase_ids:
                    raise ValueError(
                        f"Phase '{phase.id}' depends on '{dep}' "
                        f"which references nonexistent phase"
                    )

        return self
