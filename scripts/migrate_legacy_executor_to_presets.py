# /// script
# requires-python = ">=3.11"
# dependencies = ["ruamel.yaml"]
# ///
"""One-shot migrator: legacy executor:/default_model:/Node.model: → settings.presets:.

The preset rework replaced the collapsed ``executor:`` enum with named
``settings.presets``. This is a **one-shot** migration — once every
workflow on disk is converted it has no further use, hence a standalone
script rather than a path baked into ``sqrlly.cli.migrate`` (which
handles the older, longer-lived phases→nodes / quality_gate→evaluation
transforms).

Scope: operates on YAML that is already in post-cutover *node* shape
(``nodes:`` / ``execute:`` / ``fan_out:``). For pre-Stage-4 YAML, run
the base migration in ``sqrlly.cli.migrate`` first.

Usage:
    uv run scripts/migrate_legacy_executor_to_presets.py <file.yaml> [--dry-run]

Without ``--dry-run`` the file is rewritten in place (comments,
anchors, and templated strings preserved via ruamel round-trip mode).
Idempotent — running on already-migrated YAML is a no-op.

Transform:
  - ``settings.executor:`` + ``settings.default_model:`` → a
    ``settings.presets:`` block. The executor maps to
    ``(transport, provider)``; one preset per unique model in use.
  - ``Node.model:`` / ``params.model:`` overrides → ``params.preset:``
    references. Gate-only nodes (no ``execute:`` block) can't carry
    params — their override is dropped and the loss is reported.
  - Pure-script workflows (no prompt dispatch) lose the vestigial
    ``executor:`` / ``default_model:`` keys without gaining presets.
"""
from __future__ import annotations

import sys
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq


def _yaml() -> YAML:
    """Round-trip-mode YAML preserving comments, anchors, and formatting.

    Deliberately duplicated from ``sqrlly/cli/migrate.py``: this file is
    a PEP-723 standalone (``uv run``-able with no install) and cannot
    import from the ``sqrlly`` package.
    """
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 200
    y.indent(mapping=2, sequence=4, offset=2)
    return y


class MigrateError(ValueError):
    """Raised on an unmigratable construct."""


_PROMPT_EXTS = (".md", ".txt", ".prompt")
_EXECUTOR_TO_TRANSPORT_PROVIDER = {
    "acp": ("acp", "anthropic"),
    "anthropic": ("api", "anthropic"),
    "openai": ("api", "openai"),
    "deepseek": ("api", "deepseek"),
    "custom": ("api", "custom"),
}


def _node_is_prompt_dispatch(node: CommentedMap) -> bool:
    """True iff this node fires an LLM prompt — ``execute.url`` ends in a
    prompt extension OR ``execute.mode == "prompt"``."""
    execute = node.get("execute")
    if not isinstance(execute, CommentedMap):
        return False
    if execute.get("mode") == "prompt":
        return True
    url = execute.get("url")
    if not isinstance(url, str):
        return False
    lowered = url.lower()
    return any(lowered.endswith(ext) for ext in _PROMPT_EXTS)


def _workflow_has_prompt_dispatch(root: CommentedMap) -> bool:
    """True iff any node in the workflow does prompt-mode dispatch."""
    nodes = root.get("nodes")
    if not isinstance(nodes, CommentedSeq):
        return False
    for node in nodes:
        if isinstance(node, CommentedMap) and _node_is_prompt_dispatch(node):
            return True
        if isinstance(node, CommentedMap):
            fan_out = node.get("fan_out")
            if isinstance(fan_out, CommentedMap):
                template = fan_out.get("template")
                if isinstance(template, CommentedMap) and _node_is_prompt_dispatch(template):
                    return True
    return False


def _sanitize_model_name(model: str) -> str:
    """Make a model identifier safe for use as a preset name key."""
    return "".join(c if c.isalnum() else "_" for c in model).strip("_")


def _walk_model_holders(root: CommentedMap):
    """Yield ``(node_like, path)`` for every mapping that can carry a
    ``model:`` field or ``execute.params.model``: top-level nodes,
    fan_out templates, fan_out final_nodes."""
    nodes = root.get("nodes")
    if not isinstance(nodes, CommentedSeq):
        return
    for node in nodes:
        if not isinstance(node, CommentedMap):
            continue
        node_id = str(node.get("id", "?"))
        yield node, f"nodes[{node_id}]"
        fan_out = node.get("fan_out")
        if not isinstance(fan_out, CommentedMap):
            continue
        template = fan_out.get("template")
        if isinstance(template, CommentedMap):
            yield template, f"nodes[{node_id}].fan_out.template"
        finals = fan_out.get("final_nodes")
        if isinstance(finals, CommentedSeq):
            for final in finals:
                if isinstance(final, CommentedMap):
                    fid = str(final.get("id", "?"))
                    yield final, f"nodes[{node_id}].fan_out.final_nodes[{fid}]"


def _node_model_refs(node_like: CommentedMap) -> tuple[str | None, str | None]:
    """Read ``(params.model, node.model)`` from a node-shape mapping
    without mutating either."""
    params_model: str | None = None
    exe = node_like.get("execute")
    if isinstance(exe, CommentedMap):
        params = exe.get("params")
        if isinstance(params, CommentedMap):
            pm = params.get("model")
            if isinstance(pm, str):
                params_model = pm
    node_model_val = node_like.get("model")
    node_model = node_model_val if isinstance(node_model_val, str) else None
    return params_model, node_model


def _collect_models_in_use(root: CommentedMap, default_model: str) -> list[str]:
    """Models referenced by default_model + any Node.model + any
    params.model, deduped and ordered (default_model first)."""
    seen: list[str] = []
    if default_model and default_model not in seen:
        seen.append(default_model)
    for node_like, _path in _walk_model_holders(root):
        for model in _node_model_refs(node_like):
            if model is not None and model not in seen:
                seen.append(model)
    return seen


def _rewrite_node_model_to_preset(
    node: CommentedMap,
    default_model: str,
    model_to_preset: dict[str, str],
    path: str,
    changes: list[str],
) -> None:
    """Replace ``Node.model`` + ``params.model`` with a ``params.preset:``
    reference. If the model matches default_model, drop the field
    entirely (the default preset applies)."""
    chosen_model: str | None = None
    chosen_source: str | None = None
    exe = node.get("execute")
    if isinstance(exe, CommentedMap):
        params = exe.get("params")
        if isinstance(params, CommentedMap) and "model" in params:
            chosen_model = params.pop("model")
            chosen_source = "params.model"
    if chosen_model is None and "model" in node:
        chosen_model = node.pop("model")
        chosen_source = "node.model"
    if chosen_model is None or chosen_model == default_model:
        return

    preset_name = model_to_preset.get(chosen_model)
    if preset_name is None:
        return

    # Gate-only nodes (no `execute:`) can't carry params — the override
    # is dropped at migrate time. Surface it so the operator sees the
    # lost semantics (gate falls back to the default preset's model).
    if not isinstance(exe, CommentedMap):
        changes.append(
            f"{path}: {chosen_source}={chosen_model!r} dropped (gate-only "
            f"node has no execute: block to hold params.preset); gate "
            f"will use default preset's model ({default_model!r})"
        )
        return
    params = exe.get("params")
    if not isinstance(params, CommentedMap):
        params = CommentedMap()
        exe["params"] = params
    params["preset"] = preset_name
    changes.append(
        f"{path}: {chosen_source}={chosen_model!r} → params.preset={preset_name!r}"
    )


def migrate_legacy_executor_to_presets(
    root: CommentedMap, changes: list[str],
) -> None:
    """Rewrite legacy executor:/default_model:/Node.model: into a
    settings.presets block + per-node params.preset references.

    Idempotent: if ``settings.presets`` already exists, no-op.
    Pure-script workflows just lose the vestigial fields.
    """
    settings = root.get("settings")
    if not isinstance(settings, CommentedMap):
        settings = None

    if settings is not None and "presets" in settings:
        return

    has_prompt = _workflow_has_prompt_dispatch(root)
    legacy_executor = settings.get("executor") if settings else None
    legacy_default_model = settings.get("default_model") if settings else None

    if not has_prompt:
        if settings is not None:
            if "executor" in settings:
                settings.pop("executor")
                changes.append("settings.executor: removed (pure-script workflow)")
            if "default_model" in settings:
                settings.pop("default_model")
                changes.append("settings.default_model: removed (pure-script workflow)")
        nodes = root.get("nodes")
        if isinstance(nodes, CommentedSeq):
            for node in nodes:
                if isinstance(node, CommentedMap) and "model" in node:
                    node.pop("model")
        return

    executor_key = legacy_executor or "anthropic"
    if executor_key not in _EXECUTOR_TO_TRANSPORT_PROVIDER:
        raise MigrateError(
            f"Unknown legacy executor: {executor_key!r}; "
            f"supported: {sorted(_EXECUTOR_TO_TRANSPORT_PROVIDER)!r}"
        )
    transport, provider = _EXECUTOR_TO_TRANSPORT_PROVIDER[executor_key]
    default_model = legacy_default_model or "sonnet"

    models = _collect_models_in_use(root, default_model)
    model_to_preset: dict[str, str] = {}
    presets = CommentedMap()
    for model in models:
        name = (
            "default" if model == default_model
            else f"_auto_{_sanitize_model_name(model)}"
        )
        model_to_preset[model] = name
        entry = CommentedMap()
        entry["transport"] = transport
        entry["provider"] = provider
        entry["model"] = model
        if model == default_model:
            entry["default"] = True
        presets[name] = entry

    if settings is None:
        settings = CommentedMap()
        root["settings"] = settings
    if "executor" in settings:
        settings.pop("executor")
    if "default_model" in settings:
        settings.pop("default_model")
    settings["presets"] = presets

    for node_like, path in _walk_model_holders(root):
        _rewrite_node_model_to_preset(
            node_like, default_model, model_to_preset, path, changes,
        )

    changes.append(
        f"legacy executor: + default_model: → settings.presets: "
        f"({len(presets)} preset(s); default model={default_model!r})"
    )


def migrate_text(text: str) -> tuple[str, list[str]]:
    """Migrate a YAML document. Returns ``(rewritten_text, changes)``."""
    import io

    yaml = _yaml()
    data = yaml.load(text)
    if data is None or not isinstance(data, CommentedMap):
        return text, []
    changes: list[str] = []
    migrate_legacy_executor_to_presets(data, changes)
    if not changes:
        return text, []
    buf = io.StringIO()
    yaml.dump(data, buf)
    return buf.getvalue(), changes


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("-")]
    dry_run = "--dry-run" in argv
    if len(args) != 1:
        print(
            "usage: migrate_legacy_executor_to_presets.py <file.yaml> [--dry-run]",
            file=sys.stderr,
        )
        return 2

    path = Path(args[0])
    if not path.is_file():
        print(f"error: not a file: {path}", file=sys.stderr)
        return 2

    rewritten, changes = migrate_text(path.read_text())
    if not changes:
        print(f"No changes needed for {path}", file=sys.stderr)
        return 0

    for c in changes:
        print(f"  - {c}", file=sys.stderr)
    if dry_run:
        print(rewritten, end="")
    else:
        path.write_text(rewritten)
        print(f"Wrote {len(changes)} change(s) to {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
