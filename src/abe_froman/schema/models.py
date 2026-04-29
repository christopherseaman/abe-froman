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


class JoinExecution(BaseModel):
    """No-op execution that exists purely as a topology marker.

    A Node with `execution: { type: join }` runs no work — it dispatches
    only after all `depends_on` predecessors complete and produces an
    empty output. Useful for naming a synchronization point at fan-in.
    Multi-predecessor nodes implicit-join automatically; this is the
    explicit form for author readability.
    """
    type: Literal["join"] = "join"


Execution = Annotated[
    PromptExecution | CommandExecution | GateOnlyExecution | JoinExecution,
    Field(discriminator="type"),
]


def _normalize_prompt_shorthand(instance: Any) -> Any:
    """Convert prompt_file shorthand to PromptExecution."""
    if instance.prompt_file and instance.execution is None:
        instance.execution = PromptExecution(prompt_file=instance.prompt_file)
        instance.prompt_file = None
    return instance


class DimensionCheck(BaseModel):
    field: str
    min: float = Field(ge=0.0, le=1.0)


class Evaluation(BaseModel):
    """Evaluation configuration for a node."""
    validator: str
    threshold: float = Field(ge=0.0, le=1.0, default=0.0)
    blocking: bool = False
    max_retries: int | None = None
    model: str | None = None
    dimensions: list[DimensionCheck] | None = None


class OutputContract(BaseModel):
    base_directory: str
    required_files: list[str] = []


class FanOutTemplate(BaseModel):
    """Template for nodes spawned during fan-out over a manifest.

    Each Send branch (one per manifest item) runs the template against
    that item's context. The template is either a single prompt
    (`prompt_file`) or a recursive subgraph reference (`config:`):

    - `prompt_file`: legacy single-step template. The synthetic per-child
      node executes the prompt against parent context + manifest item.
    - `config`: per-child subgraph. The referenced YAML is loaded as a
      Graph and recursively compiled. Each Send branch invokes that
      subgraph in isolation, with `inputs:` rendered against parent
      context + manifest item and projected into the subgraph's
      node_inputs channel. The subgraph's terminal output becomes the
      child's output.

    Exactly one of {prompt_file, config} must be set.
    """
    prompt_file: str | None = None
    config: str | None = None  # subgraph reference (Stage 4c per-child)
    inputs: dict[str, str] = {}  # parent + manifest item → subgraph context
    evaluation: Evaluation | None = None

    @model_validator(mode="after")
    def exactly_one_target(self) -> Self:
        defs = sum(bool(x) for x in (self.prompt_file, self.config))
        if defs != 1:
            raise ValueError(
                "FanOutTemplate: exactly one of prompt_file or config must be set"
            )
        return self


class FanOutFinalNode(BaseModel):
    """A node that runs after fan-out completes, consuming aggregate output."""
    id: str
    name: str
    description: str | None = None
    prompt_file: str | None = None
    execution: Execution | None = None
    evaluation: Evaluation | None = None

    @model_validator(mode="after")
    def normalize_prompt_file(self) -> Self:
        return _normalize_prompt_shorthand(self)


class FanOut(BaseModel):
    """Fan-out configuration: spawn N parallel instances over a manifest."""
    enabled: bool = False
    manifest_path: str | None = None
    template: FanOutTemplate | None = None
    final_nodes: list[FanOutFinalNode] = []


class Settings(BaseModel):
    output_directory: str = "output"
    max_retries: int = 3
    default_model: str = "sonnet"
    executor: str = "stub"
    default_timeout: float | None = None
    preamble_file: str | None = None
    retry_backoff: list[float] = []
    model_downgrade_chain: list[str] = ["opus", "sonnet", "haiku"]
    max_parallel_jobs: int = 4
    per_model_limits: dict[str, int] = {}
    max_subgraph_depth: int = 10  # cap on recursive subgraph nesting (Stage 4c)


class Node(BaseModel):
    id: str
    name: str
    description: str | None = None
    model: str | None = None
    prompt_file: str | None = None
    execution: Execution | None = None
    config: str | None = None  # path to another graph YAML (Stage 4c recursion)
    inputs: dict[str, str] = {}  # parent → subgraph context projection (Stage 4c)
    outputs: dict[str, str] = {}  # subgraph terminal → parent state projection (Stage 4c)
    depends_on: list[str] = []
    evaluation: Evaluation | None = None
    output_contract: OutputContract | None = None
    fan_out: FanOut | None = None
    timeout: float | None = None

    @model_validator(mode="after")
    def normalize_and_validate(self) -> Self:
        _normalize_prompt_shorthand(self)
        defs = sum(bool(x) for x in (self.execution, self.config))
        if defs > 1:
            raise ValueError(
                f"Node '{self.id}': at most one of execution/prompt_file or config"
            )
        return self

    def effective_timeout(self, settings: Settings) -> float | None:
        if self.timeout is not None:
            return self.timeout
        return settings.default_timeout

    def effective_max_retries(self, settings: Settings) -> int:
        if self.evaluation and self.evaluation.max_retries is not None:
            return self.evaluation.max_retries
        return settings.max_retries


class Graph(BaseModel):
    name: str
    version: str
    nodes: list[Node]
    settings: Settings = Settings()

    @model_validator(mode="after")
    def validate_node_references(self) -> Self:
        node_ids = {n.id for n in self.nodes}

        if len(node_ids) != len(self.nodes):
            seen = set()
            for n in self.nodes:
                if n.id in seen:
                    raise ValueError(f"Duplicate node id: {n.id}")
                seen.add(n.id)

        for node in self.nodes:
            for dep in node.depends_on:
                if dep == node.id:
                    raise ValueError(f"Node '{node.id}' has a self-dependency")
                if dep not in node_ids:
                    raise ValueError(
                        f"Node '{node.id}' depends on '{dep}' "
                        f"which references nonexistent node"
                    )

        return self
