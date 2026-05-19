"""Pre-Stage-4a YAML migration tool.

Reads workflow YAMLs that use the old vocabulary
(``phases:`` / ``dynamic_subphases:`` / ``quality_gate:`` / ``final_phases:``)
and rewrites them in-place to the post-cutover shape
(``nodes:`` / ``fan_out:`` / ``evaluation:`` / sibling nodes with
``depends_on:``). Uses ``ruamel.yaml`` round-trip mode so comments,
anchors, references, and inline templated strings (``{{var}}``) survive.

Migration rules:

Stage 3 → Stage 4 (renames + structural):
- ``phases:`` → ``nodes:`` (key rename only).
- ``quality_gate:`` → ``evaluation:`` (key rename only; nested fields
  preserved as-is).
- ``dynamic_subphases:`` → ``fan_out:`` with structural flattening:
  - ``manifest_path`` and ``enabled`` move to top level of ``fan_out:``.
  - ``template.prompt_file`` (or ``template.execution`` / ``template.config``)
    is **lifted into the parent node itself** — fan-out spawns instances
    of the parent. Other ``template`` fields (e.g. ``evaluation``) move
    similarly.
  - ``final_phases:`` items become **separate sibling nodes** appended
    after the fan-out parent in the ``nodes:`` list, each with
    ``depends_on: [<parent_id>]``. If multiple finals chain, each later
    final depends on the previous: original ordering is preserved.

Stage 4 → Stage 5b (collapse to execute.url):
- ``prompt_file: x.md`` → ``execute: { url: x.md }``
- ``execution: { type: prompt, prompt_file }`` → ``execute: { url }``
- ``execution: { type: command, command: c, args: [...] }`` →
  ``execute: { url: <shutil.which(c)>, params: { args: [...] } }``
  (raises if ``c`` is not on $PATH)
- ``execution: { type: gate_only }`` → omit ``execute:`` (gate-only by elision)
- ``execution: { type: join }`` → ``execute: { type: "join" }``
- ``execution: { type: route, cases, else }`` →
  ``route: { cases, else }`` (top-level Node.route, no execute block)
- ``config: x.yaml`` + top-level ``inputs:`` + ``outputs:`` →
  ``execute: { url: x.yaml, params: { inputs, outputs } }``
- ``fan_out.template.prompt_file`` → ``fan_out.template.execute: { url }``
- ``FanOutFinalNode.prompt_file`` / ``.execution`` → ``.execute: ...``

Stage 5b → Stage 5c (inline route):
- ``execute: { type: route, cases, else }`` → ``route: { cases, else }``
  on the same node; the ``execute:`` block is dropped entirely.

Stage transforms chain automatically: feeding pre-Stage-4 YAML runs
all rounds. Idempotent: re-running on Stage-5c YAML is a no-op.
"""
from __future__ import annotations

import io
import shutil
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq


def _yaml() -> YAML:
    """Round-trip-mode YAML preserving comments, anchors, and formatting."""
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 200  # avoid wrapping templated strings
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _rename_key(node: CommentedMap, old: str, new: str) -> bool:
    """Rename `old` → `new` in `node`, preserving position and value.

    Returns True if a rename happened.
    """
    if old not in node or new in node:
        return False
    keys = list(node.keys())
    pos = keys.index(old)
    value = node[old]
    del node[old]
    # Insert new key at the same position
    items = list(node.items())
    # The deletion shifted subsequent items left by one; re-pos accordingly.
    items.insert(pos, (new, value))
    node.clear()
    for k, v in items:
        node[k] = v
    return True


def _migrate_evaluation_key(node: CommentedMap, changes: list[str], path: str) -> None:
    """``quality_gate:`` → ``evaluation:`` on a single node-like mapping."""
    if _rename_key(node, "quality_gate", "evaluation"):
        changes.append(f"{path}: quality_gate → evaluation")


def _migrate_dynamic_subphases(
    parent_node: CommentedMap, parent_idx: int, parent_id: str,
    changes: list[str], path: str,
) -> list[CommentedMap]:
    """Rewrite a parent node that has ``dynamic_subphases:``.

    Mutates `parent_node` in place; returns a list of new sibling nodes
    (lifted from ``final_phases:``) that the caller must insert after
    `parent_idx` in the ``nodes:`` list.
    """
    if "dynamic_subphases" not in parent_node:
        return []

    ds: CommentedMap = parent_node.pop("dynamic_subphases")
    fan_out: CommentedMap = CommentedMap()

    # manifest_path and enabled lift to top of fan_out
    for key in ("enabled", "manifest_path"):
        if key in ds:
            fan_out[key] = ds.pop(key)

    # template lifts the executable definition (prompt_file / execution / config)
    # AND nested evaluation/output_contract into the parent node and a fan_out template
    template = ds.pop("template", None) if "template" in ds else None
    if template is not None:
        # Per the new schema, FanOutTemplate keeps {prompt_file, evaluation}.
        # The rest of template's executable definition (execution: / config:
        # if any) lifts onto the parent node so fan-out spawns instances of
        # the parent itself. quality_gate inside template renames here too.
        if isinstance(template, CommentedMap):
            _migrate_evaluation_key(template, changes, f"{path}.dynamic_subphases.template")
            fan_out_template: CommentedMap = CommentedMap()
            for tk in list(template.keys()):
                tv = template[tk]
                if tk in ("prompt_file", "evaluation"):
                    fan_out_template[tk] = tv
                else:
                    # execution: / config: / model: / etc. → lift to parent
                    if tk not in parent_node:
                        parent_node[tk] = tv
            if fan_out_template:
                fan_out["template"] = fan_out_template

    # final_phases lift to sibling nodes with depends_on chain
    siblings: list[CommentedMap] = []
    final_phases = ds.pop("final_phases", None) if "final_phases" in ds else None
    if final_phases is not None and isinstance(final_phases, CommentedSeq):
        prev_id = parent_id
        for fp in final_phases:
            if not isinstance(fp, CommentedMap):
                continue
            sibling: CommentedMap = CommentedMap()
            for k in list(fp.keys()):
                sibling[k] = fp[k]
            _migrate_evaluation_key(sibling, changes, f"{path}.final_phases[{sibling.get('id', '?')}]")
            sibling["depends_on"] = [prev_id]
            siblings.append(sibling)
            prev_id = str(sibling.get("id", prev_id))

    # Anything else left on ds goes onto fan_out (forward-compat)
    for leftover in list(ds.keys()):
        fan_out[leftover] = ds[leftover]

    parent_node["fan_out"] = fan_out
    changes.append(f"{path}: dynamic_subphases → fan_out (template lifted, {len(siblings)} final_phases → siblings)")
    return siblings


class MigrateError(ValueError):
    """Raised when migrate cannot translate a node (e.g. command not on $PATH)."""


def _resolve_command_to_url(command: str) -> str:
    """Stage 4 ``command:`` → Stage 5b ``url:`` using shutil.which.

    Absolute paths pass through unchanged. Bare commands resolve via
    $PATH at migrate time; if not found, raises MigrateError so the
    user sees the failure rather than getting a silently broken YAML.
    """
    if command.startswith("/"):
        return command
    found = shutil.which(command)
    if found is None:
        raise MigrateError(
            f"Cannot migrate command {command!r}: not found on $PATH. "
            f"Hand-edit to an absolute path (e.g. /usr/bin/{command}) "
            f"or place the binary on PATH and re-run migrate."
        )
    return found


def _build_execute_from_execution(
    execution: CommentedMap, path: str,
) -> tuple[CommentedMap | None, str]:
    """Convert a Stage-4 ``execution:`` dict to a Stage-5b ``execute:`` dict.

    Returns ``(execute_map_or_None, change_description)``. None signals
    "elide entirely" (gate_only nodes have no execute: block in the new
    shape).
    """
    exec_type = execution.get("type")
    new_execute: CommentedMap = CommentedMap()

    if exec_type == "prompt":
        prompt_file = execution.get("prompt_file")
        if not prompt_file:
            raise MigrateError(f"{path}: execution type=prompt missing prompt_file")
        new_execute["url"] = prompt_file
        return new_execute, f"{path}: execution type=prompt → execute.url"

    if exec_type == "command":
        cmd = execution.get("command")
        if not cmd:
            raise MigrateError(f"{path}: execution type=command missing command")
        new_execute["url"] = _resolve_command_to_url(cmd)
        args = execution.get("args")
        if args:
            params: CommentedMap = CommentedMap()
            params["args"] = args
            new_execute["params"] = params
        return new_execute, f"{path}: execution type=command → execute.url=<shutil.which({cmd})>"

    if exec_type == "gate_only":
        return None, f"{path}: execution type=gate_only → elided"

    if exec_type == "join":
        new_execute["type"] = "join"
        return new_execute, f"{path}: execution type=join → execute.type=join"

    if exec_type == "route":
        new_execute["type"] = "route"
        if "cases" in execution:
            new_execute["cases"] = execution["cases"]
        if "else" in execution:
            new_execute["else"] = execution["else"]
        return new_execute, f"{path}: execution type=route → execute.type=route"

    raise MigrateError(f"{path}: unknown execution type {exec_type!r}")


def _migrate_node_to_execute(
    node: CommentedMap, changes: list[str], path: str,
) -> None:
    """Stage 4 → Stage 5b transform on a single node-shaped mapping.

    Mutates the node in place. Handles all six legacy executable shapes:
    prompt_file shorthand, execution discriminated union (5 types),
    config + inputs/outputs subgraph reference. Idempotent — if the node
    already has ``execute:``, this is a no-op.
    """
    if "execute" in node:
        return  # already migrated

    # Subgraph reference: config + (optional) inputs/outputs
    if "config" in node:
        new_execute: CommentedMap = CommentedMap()
        new_execute["url"] = node.pop("config")
        params: CommentedMap = CommentedMap()
        if "inputs" in node:
            params["inputs"] = node.pop("inputs")
        if "outputs" in node:
            params["outputs"] = node.pop("outputs")
        if params:
            new_execute["params"] = params
        node["execute"] = new_execute
        changes.append(f"{path}: config + inputs/outputs → execute.url + params")
        return

    # Discriminated-union execution
    if "execution" in node:
        execution = node.get("execution")
        if isinstance(execution, CommentedMap):
            new_execute, change = _build_execute_from_execution(execution, path)
            del node["execution"]
            if new_execute is not None:
                node["execute"] = new_execute
            changes.append(change)
            return

    # prompt_file shorthand
    if "prompt_file" in node:
        prompt_file = node.pop("prompt_file")
        new_execute2: CommentedMap = CommentedMap()
        new_execute2["url"] = prompt_file
        node["execute"] = new_execute2
        changes.append(f"{path}: prompt_file → execute.url")


def _migrate_legacy_route_to_inline(
    node: CommentedMap, changes: list[str], path: str,
) -> None:
    """Stage 5b → Stage 5c transform: lift ``execute: { type: route }``
    to a top-level ``route:`` block on the same node, dropping the
    ``execute:`` block entirely.

    Idempotent: if no legacy route execute is present, returns unchanged.
    If the node already has a ``route:`` AND a legacy ``execute: route``,
    that's a mistake — log a warning and skip (validators downstream
    will surface it to the user).
    """
    execute = node.get("execute")
    if not isinstance(execute, CommentedMap):
        return
    if execute.get("type") != "route":
        return

    if "route" in node:
        # Both shapes set on the same node — the schema validator already
        # forbids this combination. Skip the migration and let the
        # validator surface the error rather than silently overwriting.
        changes.append(
            f"{path}: WARNING — node has both legacy `execute: type=route` "
            f"and inline `route:`; skipping route migration (resolve manually)"
        )
        return

    new_route: CommentedMap = CommentedMap()
    if "cases" in execute:
        new_route["cases"] = execute["cases"]
    if "else" in execute:
        new_route["else"] = execute["else"]

    # Replace `execute:` with `route:` at the same key position so the
    # rewrite preserves field ordering and any surrounding comments.
    keys = list(node.keys())
    pos = keys.index("execute")
    items = list(node.items())
    items.pop(pos)
    items.insert(pos, ("route", new_route))
    node.clear()
    for k, v in items:
        node[k] = v
    changes.append(f"{path}: execute.type=route → route (inline)")


def _migrate_fan_out_template_to_execute(
    fan_out: CommentedMap, changes: list[str], path: str,
) -> None:
    """Migrate fan_out.template's executable definition + final_nodes."""
    template = fan_out.get("template")
    if isinstance(template, CommentedMap):
        _migrate_node_to_execute(template, changes, f"{path}.template")
    finals = fan_out.get("final_nodes")
    if isinstance(finals, CommentedSeq):
        for fn in finals:
            if isinstance(fn, CommentedMap):
                fn_id = str(fn.get("id", "?"))
                _migrate_node_to_execute(fn, changes, f"{path}.final_nodes[{fn_id}]")


_PROMPT_EXTS = (".md", ".txt", ".prompt")
_EXECUTOR_TO_TRANSPORT_PROVIDER = {
    "acp": ("acp", "anthropic"),
    "anthropic": ("api", "anthropic"),
    "openai": ("api", "openai"),
    "deepseek": ("api", "deepseek"),
    "custom": ("api", "custom"),
}


def _node_is_prompt_dispatch(node: CommentedMap) -> bool:
    """Heuristic: does this node fire an LLM prompt?

    True iff ``execute.url`` ends in a known prompt extension OR
    ``execute.mode == "prompt"``. False for script/binary/join/None.
    """
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
        # fan_out templates also dispatch
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


def _collect_models_in_use(root: CommentedMap, default_model: str) -> list[str]:
    """Return models referenced by default_model + any Node.model +
    any params.model, deduped and ordered (default_model first)."""
    seen: list[str] = []
    if default_model and default_model not in seen:
        seen.append(default_model)
    nodes = root.get("nodes")
    if isinstance(nodes, CommentedSeq):
        for node in nodes:
            if not isinstance(node, CommentedMap):
                continue
            nm = node.get("model")
            if isinstance(nm, str) and nm not in seen:
                seen.append(nm)
            exe = node.get("execute")
            if isinstance(exe, CommentedMap):
                params = exe.get("params")
                if isinstance(params, CommentedMap):
                    pm = params.get("model")
                    if isinstance(pm, str) and pm not in seen:
                        seen.append(pm)
            fan_out = node.get("fan_out")
            if isinstance(fan_out, CommentedMap):
                template = fan_out.get("template")
                if isinstance(template, CommentedMap):
                    tnm = template.get("model")
                    if isinstance(tnm, str) and tnm not in seen:
                        seen.append(tnm)
                    texe = template.get("execute")
                    if isinstance(texe, CommentedMap):
                        tparams = texe.get("params")
                        if isinstance(tparams, CommentedMap):
                            tpm = tparams.get("model")
                            if isinstance(tpm, str) and tpm not in seen:
                                seen.append(tpm)
                finals = fan_out.get("final_nodes")
                if isinstance(finals, CommentedSeq):
                    for final in finals:
                        if not isinstance(final, CommentedMap):
                            continue
                        fnm = final.get("model")
                        if isinstance(fnm, str) and fnm not in seen:
                            seen.append(fnm)
                        fexe = final.get("execute")
                        if isinstance(fexe, CommentedMap):
                            fparams = fexe.get("params")
                            if isinstance(fparams, CommentedMap):
                                fpm = fparams.get("model")
                                if isinstance(fpm, str) and fpm not in seen:
                                    seen.append(fpm)
    return seen


def _rewrite_node_model_to_preset(
    node: CommentedMap,
    default_model: str,
    model_to_preset: dict[str, str],
    path: str,
    changes: list[str],
) -> None:
    """Replace ``Node.model`` + ``params.model`` with ``params.preset:``
    references. If the model matches default_model, drop the field entirely
    (the default preset applies). Otherwise insert ``params.preset:``.
    """
    # Per-call resolution chain: params.model > node.model > default.
    # We collapse that into a single params.preset per node, with the
    # higher-precedence source winning.
    chosen_model: str | None = None
    chosen_source: str | None = None   # "params.model" or "node.model"
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
        # Shouldn't happen — every model in use should be in the map.
        return

    # Insert params.preset on the node's execute block. Gate-only nodes
    # (no `execute:`) can't carry params, so the override is dropped
    # at migrate time. Surface that as a change so the operator sees
    # the lost semantics — gates on these nodes will use the default
    # preset's model going forward.
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
    changes.append(f"{path}: {chosen_source}={chosen_model!r} → params.preset={preset_name!r}")


def _migrate_legacy_executor_to_presets(
    root: CommentedMap, changes: list[str],
) -> None:
    """Rewrite legacy executor:/default_model:/Node.model: into a
    settings.presets block + per-node params.preset references.

    Idempotent: if ``settings.presets`` already exists, this is a no-op.
    Pure-script workflows (no prompt dispatch) just lose the vestigial
    default_model/executor fields without gaining presets.
    """
    settings = root.get("settings")
    if not isinstance(settings, CommentedMap):
        settings = None

    # Already-migrated: skip.
    if settings is not None and "presets" in settings:
        return

    has_prompt = _workflow_has_prompt_dispatch(root)
    legacy_executor = settings.get("executor") if settings else None
    legacy_default_model = settings.get("default_model") if settings else None

    # Decide whether to build presets at all.
    if not has_prompt:
        # No prompt dispatch → just drop legacy fields if present.
        if settings is not None:
            if "executor" in settings:
                settings.pop("executor")
                changes.append("settings.executor: removed (pure-script workflow)")
            if "default_model" in settings:
                settings.pop("default_model")
                changes.append("settings.default_model: removed (pure-script workflow)")
        # Even pure-script workflows may have stray Node.model — drop those too.
        nodes = root.get("nodes")
        if isinstance(nodes, CommentedSeq):
            for node in nodes:
                if isinstance(node, CommentedMap) and "model" in node:
                    node.pop("model")
        return

    # Build presets. Default transport/provider when executor not set
    # matches the auto-detect fallback (api+anthropic).
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
        name = "default" if model == default_model else f"_auto_{_sanitize_model_name(model)}"
        model_to_preset[model] = name
        entry = CommentedMap()
        entry["transport"] = transport
        entry["provider"] = provider
        entry["model"] = model
        if model == default_model:
            entry["default"] = True
        presets[name] = entry

    # Make sure settings exists; insert presets at the end.
    if settings is None:
        settings = CommentedMap()
        root["settings"] = settings
    if "executor" in settings:
        settings.pop("executor")
    if "default_model" in settings:
        settings.pop("default_model")
    settings["presets"] = presets

    # Rewrite node-level model overrides → params.preset references.
    nodes = root.get("nodes")
    if isinstance(nodes, CommentedSeq):
        for node in nodes:
            if not isinstance(node, CommentedMap):
                continue
            node_id = str(node.get("id", "?"))
            _rewrite_node_model_to_preset(
                node, default_model, model_to_preset, f"nodes[{node_id}]", changes,
            )
            # fan_out template + final_nodes — same shape on the inside.
            fan_out = node.get("fan_out")
            if isinstance(fan_out, CommentedMap):
                template = fan_out.get("template")
                if isinstance(template, CommentedMap):
                    _rewrite_node_model_to_preset(
                        template, default_model, model_to_preset,
                        f"nodes[{node_id}].fan_out.template", changes,
                    )
                finals = fan_out.get("final_nodes")
                if isinstance(finals, CommentedSeq):
                    for final in finals:
                        if not isinstance(final, CommentedMap):
                            continue
                        final_id = str(final.get("id", "?"))
                        _rewrite_node_model_to_preset(
                            final, default_model, model_to_preset,
                            f"nodes[{node_id}].fan_out.final_nodes[{final_id}]",
                            changes,
                        )

    changes.append(
        f"legacy executor: + default_model: → settings.presets: "
        f"({len(presets)} preset(s); default model={default_model!r})"
    )


def _walk_and_migrate(root: CommentedMap, changes: list[str]) -> None:
    """Walk a parsed Graph and apply renames + restructures in place."""
    # phases: → nodes: at the top level
    if "phases" in root and "nodes" not in root:
        root["nodes"] = root.pop("phases")
        changes.append("phases → nodes")

    nodes = root.get("nodes")
    if not isinstance(nodes, CommentedSeq):
        return

    # Walk nodes; rewrite quality_gate → evaluation and dynamic_subphases → fan_out.
    # Lifted final_phases become new sibling nodes inserted in place.
    i = 0
    while i < len(nodes):
        node = nodes[i]
        if isinstance(node, CommentedMap):
            node_id = str(node.get("id", "?"))
            path = f"nodes[{node_id}]"
            _migrate_evaluation_key(node, changes, path)

            # Recurse into nested template / final_phases evaluation keys
            # before structural flattening (so the renames happen first).
            ds = node.get("dynamic_subphases")
            if isinstance(ds, CommentedMap):
                tmpl = ds.get("template")
                if isinstance(tmpl, CommentedMap):
                    _migrate_evaluation_key(tmpl, changes, f"{path}.dynamic_subphases.template")
                fps = ds.get("final_phases")
                if isinstance(fps, CommentedSeq):
                    for fp in fps:
                        if isinstance(fp, CommentedMap):
                            _migrate_evaluation_key(
                                fp, changes,
                                f"{path}.dynamic_subphases.final_phases[{fp.get('id', '?')}]",
                            )

            siblings = _migrate_dynamic_subphases(node, i, node_id, changes, path)
            for offset, sib in enumerate(siblings, start=1):
                nodes.insert(i + offset, sib)

            # Stage 5b: collapse to execute.url. Runs AFTER the Stage 3→4
            # transforms above so a single migrate invocation chains both.
            _migrate_node_to_execute(node, changes, path)
            for sib in siblings:
                sib_id = str(sib.get("id", "?"))
                _migrate_node_to_execute(sib, changes, f"nodes[{sib_id}]")

            # Stage 5c: lift legacy `execute: { type: route }` to inline
            # `route:`. Runs AFTER Stage 4→5b so freshly-translated route
            # executes also get lifted in the same migrate invocation.
            _migrate_legacy_route_to_inline(node, changes, path)
            for sib in siblings:
                sib_id = str(sib.get("id", "?"))
                _migrate_legacy_route_to_inline(sib, changes, f"nodes[{sib_id}]")

            fan_out = node.get("fan_out")
            if isinstance(fan_out, CommentedMap):
                _migrate_fan_out_template_to_execute(fan_out, changes, f"{path}.fan_out")

            i += len(siblings) + 1
        else:
            i += 1

    # Final pass: legacy executor/default_model/Node.model → settings.presets.
    # Runs LAST so freshly-translated node shapes (e.g. fan_out templates
    # lifted to execute blocks) are also walked for model references.
    _migrate_legacy_executor_to_presets(root, changes)


def migrate_yaml(text: str) -> tuple[str, list[str]]:
    """Migrate a YAML document text from pre-Stage-4a → post-cutover shape.

    Returns ``(rewritten_text, changes)``. ``changes`` is empty when the
    input is already post-cutover (idempotent).
    """
    yaml = _yaml()
    data = yaml.load(text)
    if data is None:
        return text, []

    changes: list[str] = []
    if isinstance(data, CommentedMap):
        _walk_and_migrate(data, changes)

    if not changes:
        return text, []

    buf = io.StringIO()
    yaml.dump(data, buf)
    return buf.getvalue(), changes


def migrate_file(path: Path, *, in_place: bool = False, dry_run: bool = False) -> tuple[str, list[str]]:
    """Read `path`, migrate, and either return text or write it back.

    Returns the rewritten text and the changes list. If ``in_place`` is
    True, also writes the file. ``dry_run`` is informational only — the
    caller decides what to print.
    """
    original = path.read_text()
    rewritten, changes = migrate_yaml(original)
    if in_place and changes and not dry_run:
        path.write_text(rewritten)
    return rewritten, changes
