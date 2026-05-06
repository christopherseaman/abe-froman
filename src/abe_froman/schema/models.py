from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RouteCase(BaseModel):
    """One case in a route ladder.

    `goto` accepts a single target id or a list (static fan-out via
    `Command(goto=[...])`). `include_eval` opts the goto target into
    receiving the same neutral eval-result preamble that retry
    attempts get auto-prepended to their prompt. Default off — success
    paths typically perform a different task than the previous node,
    so the previous eval's feedback is noise unless explicitly wanted.
    """
    when: str
    goto: str | list[str]
    include_eval: bool = False


class RouteElse(BaseModel):
    """Structured else: target — same fields as RouteCase minus when:."""
    goto: str | list[str]
    include_eval: bool = False


class Route(BaseModel):
    """Forward-edge dispatch on a node — Stage 5c inline routing.

    Exactly one shape is set:

    - **Unconditional shorthand**: `goto:` (str | list[str]) plus
      optional `include_eval:`. Compiles to `Command(goto=...)` from
      the source node, no predicate evaluation.
    - **Conditional ladder**: `cases:` (first-match-wins) plus
      `else:`. `else:` may be a bare string/list (auto-normalized to
      `RouteElse(goto=..., include_eval=False)`) or a structured
      RouteElse for `include_eval` control.

    Lives on `Node.route`. When the source node also has `execute:`,
    the route fires after execute (and after eval, if present)
    settles. When `execute:` is omitted, the node is a standalone
    router (replaces today's `Execute.type="route"`).
    """
    model_config = ConfigDict(populate_by_name=True)
    # Unconditional shorthand:
    goto: str | list[str] | None = None
    include_eval: bool = False
    # OR conditional ladder:
    cases: list[RouteCase] = []
    else_: RouteElse | None = Field(default=None, alias="else")

    @model_validator(mode="before")
    @classmethod
    def _normalize_else(cls, data: Any) -> Any:
        # Allow `else: <string>` and `else: [a, b]` shorthand by
        # promoting to RouteElse(goto=..., include_eval=False).
        if not isinstance(data, dict):
            return data
        else_val = data.get("else") if "else" in data else data.get("else_")
        if isinstance(else_val, (str, list)):
            promoted = {"goto": else_val, "include_eval": False}
            if "else" in data:
                data = {**data, "else": promoted}
            else:
                data = {**data, "else_": promoted}
        return data

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        has_goto = self.goto is not None
        has_cases = bool(self.cases)
        has_else = self.else_ is not None
        if has_goto:
            if has_cases or has_else:
                raise ValueError(
                    "Route shorthand `goto:` is mutually exclusive with "
                    "`cases:`/`else:` (got both)"
                )
        else:
            if not has_cases and not has_else:
                raise ValueError(
                    "Route requires either `goto:` (unconditional) "
                    "or `cases:` + `else:` (conditional ladder)"
                )
            if has_cases and not has_else:
                raise ValueError(
                    "Route with `cases:` requires `else:` target"
                )
            if self.include_eval:
                # include_eval at the route level only meaningful for
                # the goto-shorthand form; cases/else carry their own.
                raise ValueError(
                    "`include_eval` at the route level is only valid "
                    "with `goto:` shorthand; for `cases:` ladders, set "
                    "include_eval per-case (and on the else target)"
                )
        return self


ExecuteMode = Literal[
    "prompt", "subgraph", "exec",
    "python", "node", "tsx", "bash",
]


class Execute(BaseModel):
    """Stage 5b unified execution shape.

    Three orthogonal modes, exactly one of which is active per node:

      1. URL mode (`url:` set, `type:` unset) — dispatched by URL
         extension/scheme to one of: prompt, subgraph, script, exec.
         Authors can override the extension-driven choice with
         ``mode:`` when the URL doesn't carry a recognizable extension
         (or carries a misleading one).
      2. Join sentinel (`type: "join"`) — no-op topology marker.
      3. Route ladder (`type: "route"`, `cases:`, `else:`) — pure
         Command(goto=...) dispatch over structured state.
    """
    model_config = ConfigDict(populate_by_name=True)
    url: str | None = None
    type: Literal["join", "route"] | None = None
    mode: ExecuteMode | None = None
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
            if self.cases or self.else_ is not None or self.params or self.mode:
                raise ValueError(
                    "Execute type=join takes no cases, else, params, or mode"
                )
        elif self.type == "route":
            if self.else_ is None:
                raise ValueError("Execute type=route requires else: target")
            if self.params:
                raise ValueError(
                    "Execute type=route takes no params (use cases / else)"
                )
            if self.mode:
                raise ValueError(
                    "Execute type=route takes no mode (mode applies to URL mode only)"
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
    # `None` triggers auto-detect at CLI dispatch (DeepSeek key → ACP
    # via npx → stub). Explicit choices: "stub" | "acp" | "deepseek" |
    # "openai". The CLI `--executor` flag overrides this field.
    executor: str | None = None
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
    route: Route | None = None
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

        # Forbid double-spelling: a node cannot carry BOTH inline
        # `route:` AND legacy `execute: { type: route }`. They're
        # equivalent forward-edge mechanisms; pick one.
        for node in self.nodes:
            has_inline_route = node.route is not None
            has_legacy_route = (
                node.execute is not None and node.execute.type == "route"
            )
            if has_inline_route and has_legacy_route:
                raise ValueError(
                    f"Node '{node.id}' has both `route:` and "
                    f"`execute: {{ type: route }}`; pick one. "
                    f"`route:` is the canonical form; "
                    f"`execute.type=route` is deprecated."
                )

        # Validate inline-route goto/else targets resolve.
        def _check_targets(source_id: str, where: str, targets: Any) -> None:
            ids = targets if isinstance(targets, list) else [targets]
            for tgt in ids:
                if tgt == "__end__":
                    continue
                if tgt not in node_ids:
                    raise ValueError(
                        f"Route '{source_id}' {where} '{tgt}' "
                        f"references nonexistent node"
                    )

        for node in self.nodes:
            if node.route is None:
                continue
            r = node.route
            if r.goto is not None:
                _check_targets(node.id, "goto", r.goto)
            for case in r.cases:
                _check_targets(node.id, f"case[when={case.when!r}] goto", case.goto)
            if r.else_ is not None:
                _check_targets(node.id, "else goto", r.else_.goto)

        # Route nodes: identified union of legacy + inline. Both
        # dispatch via Command(goto=); neither can appear in another
        # node's `depends_on:` (would double-trigger the goto target).
        legacy_route_ids: set[str] = {
            n.id for n in self.nodes
            if n.execute is not None and n.execute.type == "route"
        }
        inline_route_ids: set[str] = {n.id for n in self.nodes if n.route is not None}
        route_ids: set[str] = legacy_route_ids | inline_route_ids

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
