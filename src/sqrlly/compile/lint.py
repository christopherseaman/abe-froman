"""Compile-time lint warnings — non-fatal author footgun checks.

Distinct from schema validation (which raises ``ValidationError``) and
graph compilation (which builds the LangGraph): these are advisory
warnings about constructs that are *valid* but likely a mistake.

Pure functions over ``Graph`` — no langgraph imports, no I/O. Top-level
config only; subgraph configs are not loaded (a subgraph is linted when
run as its own top-level workflow).
"""
from __future__ import annotations

from sqrlly.schema.models import Graph


def collect_warnings(config: Graph) -> list[str]:
    """Advisory warnings for a workflow config, in declaration order."""
    return (
        _hyphenated_id_warnings(config)
        + _advisory_gate_warnings(config)
        + _worktree_setup_exclude_warnings(config)
        + _fanout_parent_promote_warnings(config)
        + _remote_url_warnings(config)
    )


def _remote_url_warnings(config: Graph) -> list[str]:
    """SECURITY: flag any node that fetches a prompt/validator over the
    network (an ``http(s)://`` URL). Remote fetch is opt-in and gated,
    but pulling execution inputs from a remote source is a trust
    boundary — surface it loudly at ``validate``/``run`` so it can never
    be silent. (Runtime fetch also logs a warning; see runtime/url.py.)
    """
    out: list[str] = []

    def is_remote(url: object) -> bool:
        return isinstance(url, str) and url.lower().startswith(
            ("http://", "https://")
        )

    for node in config.nodes:
        if node.execute is not None and is_remote(node.execute.url):
            out.append(
                f"node '{node.id}': fetches its execution input from a "
                f"REMOTE url ({node.execute.url}) — ensure the source is "
                f"trusted; remote content runs with the orchestrator's "
                f"full privileges."
            )
        ev = node.evaluation
        if ev is not None and is_remote(getattr(ev, "validator", None)):
            out.append(
                f"node '{node.id}': gate validator is a REMOTE url "
                f"({ev.validator}) — ensure the source is trusted."
            )
    return out


def _advisory_gate_warnings(config: Graph) -> list[str]:
    """Flag a gate that can fail but won't halt.

    A node whose ``evaluation`` sets a positive ``threshold`` with
    ``blocking: false`` scores the gate but never stops the workflow — a
    below-threshold score is logged and execution continues. Easy to
    mistake a hollow "green" run for a real pass. (A ``threshold`` of 0.0
    can never fail, so it is not flagged.)
    """
    out: list[str] = []

    def check(ev: object, label: str) -> None:
        if ev is not None and not ev.blocking and ev.threshold > 0.0:
            out.append(
                f"{label}: gate sets threshold={ev.threshold} but blocking is "
                f"false — advisory only; a below-threshold score won't halt "
                f"the workflow (set 'blocking: true' to halt)."
            )

    for node in config.nodes:
        check(node.evaluation, f"node '{node.id}'")
        if node.fan_out is not None:
            template = getattr(node.fan_out, "template", None)
            if template is not None:
                check(template.evaluation, f"fan-out template of '{node.id}'")
            for fn in node.fan_out.final_nodes:
                check(fn.evaluation, f"fan-out final node '{fn.id}'")
    return out


def _worktree_setup_exclude_warnings(config: Graph) -> list[str]:
    """Flag a worktree_setup that generates an in-tree artifact (prisma
    generate) without a worktree_setup_exclude — the generated client would
    leak into the promote footprint."""
    s = config.settings
    runs_prisma_generate = any("prisma generate" in cmd for cmd in s.worktree_setup)
    if runs_prisma_generate and not s.worktree_setup_exclude:
        return [
            "settings.worktree_setup runs 'prisma generate' but "
            "worktree_setup_exclude is empty — the generated client may leak "
            "into the promote footprint. Add its output path (e.g. "
            "'src/generated/prisma' or 'node_modules') to worktree_setup_exclude."
        ]
    return []


def _fanout_parent_promote_warnings(config: Graph) -> list[str]:
    """Flag ``node.promote: true`` on a fan-out parent.

    A fan-out parent's OWN worktree holds only the manifest, not the branch
    deltas. Setting ``node.promote`` there promotes the manifest-only tree,
    not what the author almost certainly intends. ``fan_out.promote`` is the
    correct knob — it promotes each Send branch's worktree through the same
    reconcile path.
    """
    out: list[str] = []
    for node in config.nodes:
        if node.fan_out is not None and node.promote:
            out.append(
                f"node {node.id!r}: promote on a fan-out parent promotes only "
                f"its (manifest-only) worktree, not the branch worktrees — set "
                f"fan_out.promote to merge the branch deltas back to base."
            )
    return out


def _hyphenated_id_warnings(config: Graph) -> list[str]:
    """Flag node ids containing ``-``.

    A hyphenated id referenced as ``{{the-id}}`` in a Jinja template
    parses as subtraction (``the - id``), silently producing wrong or
    empty output rather than the node's value. Underscores are safe.
    Covers top-level node ids and fan-out final-node ids.
    """
    ids: list[str] = []
    for node in config.nodes:
        ids.append(node.id)
        if node.fan_out:
            ids.extend(fn.id for fn in node.fan_out.final_nodes)

    out: list[str] = []
    for node_id in ids:
        if "-" not in node_id:
            continue
        templated = "{{" + node_id + "}}"
        suggestion = node_id.replace("-", "_")
        out.append(
            f"node id '{node_id}' contains a hyphen — referenced in a "
            f"Jinja template as '{templated}' it parses as subtraction, "
            f"not a variable. Rename to '{suggestion}' if this id will "
            f"be templated."
        )
    return out
