"""Pre-Stage-4a YAML migration tool.

Reads workflow YAMLs that use the old vocabulary
(``phases:`` / ``dynamic_subphases:`` / ``quality_gate:`` / ``final_phases:``)
and rewrites them in-place to the post-cutover shape
(``nodes:`` / ``fan_out:`` / ``evaluation:`` / sibling nodes with
``depends_on:``). Uses ``ruamel.yaml`` round-trip mode so comments,
anchors, references, and inline templated strings (``{{var}}``) survive.

Migration rules:
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

Idempotency: running ``migrate_yaml`` on already-migrated YAML
produces unchanged output and an empty changes list.
"""
from __future__ import annotations

import io
from pathlib import Path

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
            i += len(siblings) + 1
        else:
            i += 1


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
