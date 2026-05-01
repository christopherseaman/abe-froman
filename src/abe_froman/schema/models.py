from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RouteCase(BaseModel):
    when: str
    goto: str


class Execute(BaseModel):
    """Stage 5b unified execution shape.

    Three orthogonal modes, exactly one of which is active per node:

      1. URL mode (`url:` set, `type:` unset) — dispatched by URL
         extension/scheme to one of: prompt, subgraph, script, exec.
      2. Join sentinel (`type: "join"`) — no-op topology marker.
      3. Route ladder (`type: "route"`, `cases:`, `else:`) — pure
         Command(goto=...) dispatch over structured state.
    """
    model_config = ConfigDict(populate_by_name=True)
    url: str | None = None
    type: Literal["join", "route"] | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    cases: list[RouteCase] = []
    else_: str | None = Field(default=None, alias="else")

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        modes_set = sum([
            self.url is not None,
            self.type == "join",
            self.type == "route",
        ])
        if modes_set != 1:
            raise ValueError(
                "Execute must set exactly one of: url, type=join, type=route "
                f"(got url={self.url!r}, type={self.type!r})"
            )
        if self.type == "join":
            if self.cases or self.else_ is not None or self.params:
                raise ValueError(
                    "Execute type=join takes no cases, else, or params"
                )
        elif self.type == "route":
            if self.else_ is None:
                raise ValueError("Execute type=route requires else: target")
            if self.params:
                raise ValueError(
                    "Execute type=route takes no params (use cases / else)"
                )
        else:  # url mode
            if self.cases or self.else_ is not None:
                raise ValueError(
                    "Execute url mode takes no cases or else (those are route-only)"
                )
        return self


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
    """Template for nodes spawned during fan-out over a manifest."""
    execute: Execute
    evaluation: Evaluation | None = None


class FanOutFinalNode(BaseModel):
    """A node that runs after fan-out completes, consuming aggregate output."""
    id: str
    name: str
    description: str | None = None
    execute: Execute | None = None
    evaluation: Evaluation | None = None


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
    # Stage 5b — execute.url remote URL gates
    base_url: str | None = None  # default base for relative urls in execute.url
    allow_remote_urls: bool = False  # master switch for non-file:// fetches
    allow_remote_scripts: bool = False  # extra opt-in for remote .py/.js/.sh
    allowed_url_hosts: list[str] = []  # fnmatch host patterns; [] = no filter
    url_headers: dict[str, dict[str, str]] = {}  # prefix → headers; ${VAR} expands
    max_remote_fetch_bytes: int = 5_000_000  # 5 MB cap


class Node(BaseModel):
    """Stage-5b node: execution is described by a single optional `execute:` block.

    Legacy fields (`prompt_file`, `execution`, `config`, `inputs`, `outputs`)
    were removed in the hard cutover. `extra="forbid"` makes that loud:
    users on pre-Stage-5b YAML get a clear ValidationError pointing at the
    unsupported key, instead of silent drop-on-the-floor.
    """
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str | None = None
    model: str | None = None
    execute: Execute | None = None
    depends_on: list[str] = []
    evaluation: Evaluation | None = None
    output_contract: OutputContract | None = None
    fan_out: FanOut | None = None
    timeout: float | None = None

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

        # Route nodes: every goto must resolve to a real node id or
        # __end__; routes must be leaves in the dep DAG (their
        # Command(goto=) IS the dispatch — a static depends_on edge
        # would double-trigger the goto target).
        route_ids: set[str] = {
            n.id for n in self.nodes
            if n.execute is not None and n.execute.type == "route"
        }
        for node in self.nodes:
            if node.execute is None or node.execute.type != "route":
                continue
            for case in node.execute.cases:
                if case.goto != "__end__" and case.goto not in node_ids:
                    raise ValueError(
                        f"Route '{node.id}' case goto '{case.goto}' "
                        f"references nonexistent node"
                    )
            else_target = node.execute.else_
            if else_target != "__end__" and else_target not in node_ids:
                raise ValueError(
                    f"Route '{node.id}' else goto '{else_target}' "
                    f"references nonexistent node"
                )
        for node in self.nodes:
            for dep in node.depends_on:
                if dep in route_ids:
                    raise ValueError(
                        f"Node '{node.id}' depends on route '{dep}'; "
                        f"routes dispatch via Command(goto=), so they "
                        f"cannot appear in another node's depends_on"
                    )

        return self
