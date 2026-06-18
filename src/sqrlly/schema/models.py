from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal, Self, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    field_validator,
    model_validator,
)


# Binary multipliers (matches `free -h`, `dd`, `psutil` reporting
# semantics — memory is power-of-2 in practice).
_BYTE_SUFFIXES: dict[str, int] = {
    "B": 1,
    "K": 1024, "KB": 1024, "KIB": 1024,
    "M": 1024**2, "MB": 1024**2, "MIB": 1024**2,
    "G": 1024**3, "GB": 1024**3, "GIB": 1024**3,
    "T": 1024**4, "TB": 1024**4, "TIB": 1024**4,
}
_BYTE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]*)\s*$")


def _parse_byte_size(value: Any) -> Any:
    """Parse a byte-size value: plain int passes through; string with
    optional suffix (``"4GB"``, ``"500MiB"``, ``"2T"``) resolves via
    :data:`_BYTE_SUFFIXES`. Suffixes are case-insensitive. Returns
    ``None`` for ``None`` (the disable sentinel)."""
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    if not isinstance(value, str):
        raise ValueError(
            f"byte size must be int or str, got {type(value).__name__}"
        )
    m = _BYTE_RE.match(value)
    if m is None:
        raise ValueError(
            f"could not parse byte size {value!r}; "
            f"expected forms like '4GB', '500MiB', '2T', or '8192'"
        )
    n_str, suffix = m.groups()
    if not suffix:
        return int(float(n_str))
    mult = _BYTE_SUFFIXES.get(suffix.upper())
    if mult is None:
        raise ValueError(
            f"unknown byte-size suffix {suffix!r}; "
            f"supported: {sorted(_BYTE_SUFFIXES)}"
        )
    return int(float(n_str) * mult)


# Worktree isolation modes. `none` is an accepted alias for `off`.
# This field is a MODE selector — a group name belongs in `worktree_group`,
# not here; non-mode tokens are rejected to catch misplaced group names.
_WORKTREE_MODES = ("auto", "isolated", "off")


def _normalize_worktree(value: Any) -> Any:
    """Normalize a worktree-mode value: ``None`` passes through (inherit);
    ``"none"`` aliases to ``"off"``; one of ``auto``/``isolated``/``off``
    is accepted; anything else raises."""
    if value is None:
        return None
    # Bare `worktree: off`/`on` parse as YAML booleans (off/no/false →
    # False, on/yes/true → True). Map the toggle to the isolation modes
    # so the ergonomic `worktree: off` opt-out works unquoted.
    if isinstance(value, bool):
        return "isolated" if value else "off"
    if not isinstance(value, str):
        raise ValueError(
            f"worktree must be a string, got {type(value).__name__}"
        )
    mode = value.strip().lower()
    if mode == "none":
        return "off"
    if mode not in _WORKTREE_MODES:
        raise ValueError(
            f"worktree must be one of auto/isolated/off "
            f"(named shared-worktree groups are not yet supported), "
            f"got {value!r}"
        )
    return mode


def _check_worktree_group_exclusive(
    worktree: str | None, worktree_group: str | None
) -> None:
    """A ``worktree_group`` already implies a shared tree, so pairing it with
    an explicit ``isolated``/``off`` mode is contradictory. Shared by the
    Settings and Node after-validators."""
    if worktree_group is not None and worktree in ("isolated", "off"):
        raise ValueError(
            "worktree_group cannot be combined with worktree="
            f"{worktree!r}: a group already implies a shared tree "
            "(omit worktree, or set it to auto)"
        )


class RouteCase(BaseModel):
    """One case in a route ladder.

    `goto` accepts a single target id or a list (static fan-out via
    `Command(goto=[...])`). `include_eval` opts the goto target into
    receiving the same neutral eval-result preamble that retry
    attempts get auto-prepended to their prompt. Default off — success
    paths typically perform a different task than the previous node,
    so the previous eval's feedback is noise unless explicitly wanted.
    """
    model_config = ConfigDict(extra="forbid")
    when: str
    goto: str | list[str]
    include_eval: bool = False


class RouteElse(BaseModel):
    """Structured else: target — same fields as RouteCase minus when:."""
    model_config = ConfigDict(extra="forbid")
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
    router.
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
            if has_else and not has_cases:
                # `else:` without `cases:` would be a silent unconditional
                # redirect — confusing structure, identical to `goto:`
                # shorthand. Reject so authors pick the unambiguous form.
                raise ValueError(
                    "Route with `else:` requires `cases:` — use `goto:` "
                    "shorthand for unconditional dispatch"
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
    """Stage 5b/5c execution shape.

    Two orthogonal modes, exactly one of which is active per node:

      1. URL mode (`url:` set, `type:` unset) — dispatched by URL
         extension/scheme to one of: prompt, subgraph, script, exec.
         Authors can override the extension-driven choice with
         ``mode:`` when the URL doesn't carry a recognizable extension
         (or carries a misleading one).
      2. Join sentinel (`type: "join"`) — no-op topology marker.

    Forward-edge dispatch (``cases:`` / ``else:``) lives on
    ``Node.route`` (a separate ``Route`` block), not on Execute.
    Stage 5c lifted route out of Execute so a single node can both
    execute AND dispatch downstream from a route ladder.
    """
    model_config = ConfigDict(populate_by_name=True)
    url: str | None = None
    type: Literal["join"] | None = None
    mode: ExecuteMode | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        modes_set = sum([
            self.url is not None,
            self.type == "join",
        ])
        if modes_set != 1:
            raise ValueError(
                "Execute must set exactly one of: url, type=join "
                f"(got url={self.url!r}, type={self.type!r})"
            )
        if self.type == "join":
            if self.params or self.mode:
                raise ValueError(
                    "Execute type=join takes no params or mode"
                )
        return self


class DimensionCheck(BaseModel):
    # `threshold` matches `Evaluation.threshold`; `min` stays accepted
    # as a YAML alias for back-compat with pre-rename workflows.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    field: str
    threshold: float = Field(ge=0.0, le=1.0, alias="min")


class Evaluation(BaseModel):
    """Evaluation configuration for a node."""
    model_config = ConfigDict(extra="forbid")
    validator: str
    threshold: float = Field(ge=0.0, le=1.0, default=0.0)
    blocking: bool = False
    max_retries: int | None = None
    model: str | None = None
    dimensions: list[DimensionCheck] | None = None


class OutputContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_directory: str
    required_files: list[str] = []

    def required_paths(self) -> list[str]:
        """Required files as workdir-relative POSIX paths, with
        ``base_directory`` prepended. A single derivation so every consumer
        constructs these paths identically (used by the existence check in
        ``gates.validate_output_contract``). A ``base_directory`` of ``"."``
        collapses to the bare filename."""
        return [(Path(self.base_directory) / f).as_posix()
                for f in self.required_files]


class FanOutTemplate(BaseModel):
    """Template for nodes spawned during fan-out over a manifest."""
    model_config = ConfigDict(extra="forbid")
    execute: Execute
    evaluation: Evaluation | None = None


class FanOutFinalNode(BaseModel):
    """A node that runs after fan-out completes, consuming aggregate output."""
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    description: str | None = None
    execute: Execute | None = None
    evaluation: Evaluation | None = None
    # Enforced via the standard execution-node path (the final node runs
    # through `_make_execution_node`), so it scaffolds + validates just
    # like a top-level node's contract.
    output_contract: OutputContract | None = None


class FanOut(BaseModel):
    """Fan-out configuration: spawn N parallel instances over a manifest.

    The presence of the ``fan_out:`` block on a node IS the activation —
    there is no separate enable flag. To disable fan-out for a node,
    remove the block entirely. (The legacy ``enabled: bool`` field was
    removed in the post-Stage-5c audit because ``fan_out: { template:
    ... }`` with ``enabled: false`` was a silent author footgun.)
    """
    model_config = ConfigDict(extra="forbid")
    manifest_path: str | None = None
    template: FanOutTemplate | None = None
    final_nodes: list[FanOutFinalNode] = []


class LlmPreset(BaseModel):
    """Named bundle of LLM execution config — referenced by name from
    prompt nodes via ``params.preset:`` (or the ``default: true`` one).

    The ``--preset`` CLI flag overrides at run time. Resolution order:
    CLI flag > ``params.preset:`` > the preset marked ``default: true``.

    Two transports are supported today, both driving Claude Code:

    - ``transport: acp`` — the ``claude-code-acp`` adapter (warm
      process, streaming chunks, MCP-capable).
    - ``transport: cli`` — subprocess-per-call ``claude -p`` (no warm
      state, real ``asyncio`` parallelism per call).

    Both currently pair with ``provider: anthropic`` because both
    invoke Claude Code under the hood; the transport choice determines
    invocation shape, not vendor. The api transport (direct Anthropic /
    OpenAI / DeepSeek) was removed in the 0.2 strip experiment.
    Additional ``cli`` providers (codex / gemini / custom) are
    on the roadmap (TODO 36); restoring a transport means
    extending these literals AND adding a factory builder row.
    """
    model_config = ConfigDict(extra="forbid")

    kind: Literal["llm"] = "llm"
    transport: Literal["acp", "cli"]
    provider: Literal["anthropic"]
    model: str
    default: bool = False

    # Tool-use permissions. Unified shape across transports, but the
    # fidelity differs: `cli` maps to exact `claude` flags; `acp` gates
    # by tool *kind* (read/edit/execute/…) in its permission callback,
    # so `permission_mode` is the portable knob and the tool lists are
    # exact on cli / best-effort (kind+title match) on acp. All unset →
    # today's behavior (cli: no tools; acp: allow-all).
    permission_mode: Literal[
        "default", "acceptEdits", "bypassPermissions", "plan"
    ] | None = None
    allowed_tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    # Escape hatch: extra args appended verbatim to the `claude` argv
    # (cli only) for anything the fields above don't cover.
    cli_args: list[str] | None = None


class CommandPreset(BaseModel):
    """Named interpreter/command bundle — referenced by name from script
    nodes via ``params.preset:``. Replaces the hardwired
    extension→interpreter map for nodes that opt in.

    ``command`` is a command string (``"uv run"``, ``"python3.12 -X dev"``,
    ``"deno run"``). At dispatch the command is ``shlex.split``, then the
    resolved script path + ``params.args`` are appended — unless the
    command contains the literal token ``{{file}}`` and/or ``{{args}}``,
    in which case those tokens are substituted in place (token-level, not
    string interpolation). Command presets carry no ``default`` — script
    nodes opt in by name; no-preset scripts use the extension map.
    """
    model_config = ConfigDict(extra="forbid")

    kind: Literal["command"] = "command"
    command: str


def _preset_discriminator(v: Any) -> str:
    """Discriminator for the Preset union. Absent ``kind`` defaults to
    ``"llm"`` so pre-command-preset YAML (which has no ``kind:`` field)
    keeps parsing as an LlmPreset."""
    if isinstance(v, dict):
        return v.get("kind", "llm")
    return getattr(v, "kind", "llm")


# A preset is either an LLM bundle or a command bundle. Pydantic
# dispatches on ``kind`` (callable discriminator → absent kind = llm).
Preset = Annotated[
    Union[
        Annotated[LlmPreset, Tag("llm")],
        Annotated[CommandPreset, Tag("command")],
    ],
    Discriminator(_preset_discriminator),
]


class Settings(BaseModel):
    output_directory: str = "output"
    max_retries: int = 3
    default_timeout: float | None = None
    preamble_file: str | None = None
    retry_backoff: list[float] = []
    model_downgrade_chain: list[str] = ["opus", "sonnet", "haiku"]
    max_parallel_jobs: int = 4
    per_model_limits: dict[str, int] = {}
    # Memory back-pressure (two complementary forms; both default
    # ``None`` = disabled, AND-composed when both are set):
    #
    # - ``memory_threshold_pct`` — block new dispatches while host
    #   memory percent is ABOVE this value (`psutil.virtual_memory()
    #   .percent`). Useful for "don't run when we're close to OOM"
    #   regardless of total RAM size.
    # - ``memory_min_available_bytes`` — block new dispatches while
    #   available bytes are BELOW this value
    #   (`psutil.virtual_memory().available`). Useful for "always
    #   keep ≥X GB free" on heterogeneous hosts. Accepts either a
    #   raw int (bytes) OR a string with a binary-multiplier suffix
    #   (``"4GB"``, ``"500MiB"``, ``"2T"``, etc.) — case-insensitive,
    #   binary semantics (KB = 1024) matching ``free -h`` / ``psutil``.
    #
    # Both compose (AND) with ``max_parallel_jobs`` /
    # ``per_model_limits`` — every gate must allow dispatch. In-flight
    # jobs are never aborted by these gates; only new acquisitions wait.
    memory_threshold_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    memory_min_available_bytes: int | None = None

    @field_validator("memory_min_available_bytes", mode="before")
    @classmethod
    def _normalize_memory_bytes(cls, v: Any) -> Any:
        return _parse_byte_size(v)

    # Worktree isolation default for the graph; inherits graph→subgraph via
    # `merge_settings`. `auto` = isolate per-node iff in a git repo (today's
    # implicit behavior, made explicit). Per-node override: `Node.worktree`.
    worktree: Literal["auto", "isolated", "off"] = "auto"
    worktree_group: str | None = None
    worktree_gc: Literal["never", "on_success"] = "never"

    @field_validator("worktree", mode="before")
    @classmethod
    def _normalize_worktree_setting(cls, v: Any) -> Any:
        return _normalize_worktree(v)

    @model_validator(mode="after")
    def _worktree_group_exclusive(self) -> "Settings":
        _check_worktree_group_exclusive(self.worktree, self.worktree_group)
        return self

    max_subgraph_depth: int = 10  # cap on recursive subgraph nesting (Stage 4c)
    # Stage 5b — execute.url remote URL gates
    base_url: str | None = None  # default base for relative urls in execute.url
    allow_remote_urls: bool = False  # master switch for non-file:// fetches
    allow_remote_scripts: bool = False  # extra opt-in for remote .py/.js/.sh
    allowed_url_hosts: list[str] = []  # fnmatch host patterns; [] = no filter
    url_headers: dict[str, dict[str, str]] = {}  # prefix → headers; ${VAR} expands
    max_remote_fetch_bytes: int = 5_000_000  # 5 MB cap
    # Named presets — workflow-level bundles of execution config.
    # Nodes reference one by name via ``params.preset:``; otherwise
    # the preset marked ``default: true`` applies. Empty is valid for
    # script-only workflows (no LLM dispatch); sqrlly does not
    # synthesize defaults from the environment. LLM dispatch with no
    # preset wired fails at the call site.
    presets: dict[str, Preset] = {}

    @model_validator(mode="after")
    def _validate_default_preset(self) -> Self:
        """Exactly one LlmPreset must be ``default: true`` when any
        LlmPreset is declared. CommandPresets have no ``default`` —
        script nodes opt in by name, so a default never applies to them.
        """
        llm_presets = {
            name: p for name, p in self.presets.items()
            if isinstance(p, LlmPreset)
        }
        if not llm_presets:
            return self
        defaults = [name for name, p in llm_presets.items() if p.default]
        if len(defaults) == 0:
            raise ValueError(
                f"settings.presets has {len(llm_presets)} LLM preset(s) "
                f"({sorted(llm_presets)!r}) but none marked "
                f"default: true — exactly one must be the default"
            )
        if len(defaults) > 1:
            raise ValueError(
                f"settings.presets has multiple default: true LLM presets "
                f"({sorted(defaults)!r}); exactly one allowed"
            )
        return self


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
    execute: Execute | None = None
    depends_on: list[str] = []
    evaluation: Evaluation | None = None
    output_contract: OutputContract | None = None
    fan_out: FanOut | None = None
    route: Route | None = None
    timeout: float | None = None
    worktree: Literal["auto", "isolated", "off"] | None = None
    worktree_group: str | None = None
    # Apply this node's worktree git delta to the base workdir after a clean
    # run. Wired for top-level nodes only (not fan-out children / subgraph
    # inner nodes). See cli/main.py run flow.
    promote: bool = False

    @field_validator("worktree", mode="before")
    @classmethod
    def _normalize_worktree_override(cls, v: Any) -> Any:
        return _normalize_worktree(v)

    @model_validator(mode="after")
    def _worktree_group_exclusive(self) -> "Node":
        _check_worktree_group_exclusive(self.worktree, self.worktree_group)
        return self

    def effective_timeout(self, settings: Settings) -> float | None:
        if self.timeout is not None:
            return self.timeout
        return settings.default_timeout

    def effective_worktree(self, settings: Settings) -> tuple[str, str | None]:
        """Resolve isolation by scope specificity. Returns (kind, group):
        kind in {auto, isolated, off, group}; group is the shared-tree name
        when kind == "group", else None."""
        if self.worktree_group is not None:
            return ("group", self.worktree_group)
        if self.worktree is not None:
            return (self.worktree, None)
        if settings.worktree_group is not None:
            return ("group", settings.worktree_group)
        return (settings.worktree, None)

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
    def _validate_unique_ids(self) -> Self:
        seen: set[str] = set()
        for n in self.nodes:
            if n.id in seen:
                raise ValueError(f"Duplicate node id: {n.id}")
            seen.add(n.id)
        return self

    @model_validator(mode="after")
    def _validate_depends_on(self) -> Self:
        node_ids = {n.id for n in self.nodes}
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

    @model_validator(mode="after")
    def _validate_route_targets(self) -> Self:
        node_ids = {n.id for n in self.nodes}

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
                _check_targets(
                    node.id, f"case[when={case.when!r}] goto", case.goto,
                )
            if r.else_ is not None:
                _check_targets(node.id, "else goto", r.else_.goto)
        return self

    @model_validator(mode="after")
    def _validate_routes_not_depended_on(self) -> Self:
        # Inline-route nodes dispatch via Command(goto=); they cannot
        # appear in another node's `depends_on:` (would double-trigger
        # the goto target).
        inline_route_ids: set[str] = {
            n.id for n in self.nodes if n.route is not None
        }
        for node in self.nodes:
            for dep in node.depends_on:
                if dep in inline_route_ids:
                    raise ValueError(
                        f"Node '{node.id}' depends on route '{dep}'; "
                        f"routes dispatch via Command(goto=), so they "
                        f"cannot appear in another node's depends_on"
                    )
        return self

    @model_validator(mode="after")
    def _validate_preset_refs(self) -> Self:
        """Every ``params.preset:`` reference must name a preset
        declared in ``settings.presets`` (or rely on the auto-detect
        default when presets is empty).

        Without this, a typo (``params: {preset: 'smrt'}``) silently
        survives validate-time and crashes the first time the node
        runs with ``RuntimeError("no backend is registered")``.
        Subgraph nodes are validated against the subgraph's own
        ``settings.presets`` (merged settings are computed at runtime;
        we only see the leaf-level declaration here).
        """
        declared = set(self.settings.presets)
        for node in self.nodes:
            if node.execute is None:
                continue
            preset_ref = node.execute.params.get("preset")
            if preset_ref is None:
                continue
            if not declared:
                raise ValueError(
                    f"Node '{node.id}' references preset {preset_ref!r} "
                    f"but settings.presets is empty"
                )
            if preset_ref not in declared:
                raise ValueError(
                    f"Node '{node.id}' references preset {preset_ref!r} "
                    f"which is not declared in settings.presets "
                    f"(declared: {sorted(declared)!r})"
                )
            # A command preset fully specifies the interpreter — it and
            # execute.mode (the handler/interpreter selector) are
            # mutually exclusive. Setting both is contradictory.
            referenced = self.settings.presets[preset_ref]
            if (
                isinstance(referenced, CommandPreset)
                and node.execute.mode is not None
            ):
                raise ValueError(
                    f"Node '{node.id}' references command preset "
                    f"{preset_ref!r} AND sets execute.mode="
                    f"{node.execute.mode!r} — mutually exclusive; the "
                    f"command preset already specifies the interpreter. "
                    f"Drop execute.mode."
                )
        return self
