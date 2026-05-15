#!/bin/bash
# Regenerate the checked-in HTML views + reference JSONL logs for the
# shipped examples. Run from the repo root.
#
# Authoring views are generated for all example workflows that have a
# directory (jokes, route_classify, pipeline_style, wave_planner,
# absurd-paper) and saved as `<example_dir>/view.html`.
#
# Debug views are generated only for fully-deterministic script-only
# examples (route_classify, pipeline_style, wave_planner). For each:
#   1. Run the workflow, capturing the JSONL log to
#      `<example_dir>/reference-run.jsonl`.
#   2. Render `<example_dir>/view-debug.html` from that log.
#
# LLM-driven examples (jokes, absurd-paper) get only authoring views;
# debug views require the appropriate API key + live API call and
# would produce a different output every time.
#
# These files are point-in-time snapshots — the JSONL has timestamps
# that change per run, so re-running this script will always produce
# a diff. Refresh intentionally, not automatically.

set -euo pipefail

cd "$(dirname "$0")/.."

# ---- authoring views (no --log) ---------------------------------------

for ex in jokes route_classify pipeline_style wave_planner absurd-paper; do
    echo "=== authoring view: $ex ==="
    uv run sqrlly view \
        "examples/$ex/workflow.yaml" \
        --out "examples/$ex/view.html"
done

# ---- debug views for deterministic examples ---------------------------

# JsonlLogger opens its destination in append mode (so live workflow
# runs preserve event history across resumes). For the regenerate
# script we want a clean log per example, so delete any prior file
# before each run.

# route_classify uses repo-relative URLs (examples/route_classify/
# scripts/triage.py), so workdir must be the repo root.
echo "=== debug run: route_classify ==="
rm -f examples/route_classify/reference-run.jsonl
uv run sqrlly run \
    examples/route_classify/workflow.yaml \
    --log examples/route_classify/reference-run.jsonl
uv run sqrlly view \
    examples/route_classify/workflow.yaml \
    --log examples/route_classify/reference-run.jsonl \
    --direction LR \
    --out examples/route_classify/view-debug.html

# pipeline_style uses absolute URLs (/usr/bin/echo), so workdir is
# irrelevant to URL resolution.
echo "=== debug run: pipeline_style ==="
rm -f examples/pipeline_style/reference-run.jsonl
uv run sqrlly run \
    examples/pipeline_style/workflow.yaml \
    --log examples/pipeline_style/reference-run.jsonl
uv run sqrlly view \
    examples/pipeline_style/workflow.yaml \
    --log examples/pipeline_style/reference-run.jsonl \
    --direction LR \
    --out examples/pipeline_style/view-debug.html

# wave_planner: needs non-git workdir + script symlink. See
# examples/wave_planner/README.md.
echo "=== debug run: wave_planner ==="
rm -f examples/wave_planner/reference-run.jsonl
WAVE_DIR="$(mktemp -d -t wave-view-XXXXXX)"
ln -sf "$(pwd)/examples/wave_planner/scripts" "$WAVE_DIR/scripts"
uv run sqrlly run \
    examples/wave_planner/workflow.yaml \
    --workdir "$WAVE_DIR" \
    --log examples/wave_planner/reference-run.jsonl
uv run sqrlly view \
    examples/wave_planner/workflow.yaml \
    --log examples/wave_planner/reference-run.jsonl \
    --out examples/wave_planner/view-debug.html
rm -rf "$WAVE_DIR"

echo
echo "Done. Refresh these views intentionally — the captured JSONL"
echo "logs have wall-clock timestamps that change per run, so every"
echo "regeneration produces a diff."
