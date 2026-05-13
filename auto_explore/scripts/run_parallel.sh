#!/usr/bin/env bash
set -euo pipefail

# Parallel auto-search template.
#
# Privacy note:
#   Do not write API keys or private service URLs into this file.
#   Pass them from environment variables instead, for example:
#     export AUTO_EXPLORE_DECIDER_BASE_URL=https://your-decider-endpoint/v1
#     export DECIDER_API_KEY=your-decider-api-key
#     export AUTO_EXPLORE_EXPLORER_BASE_URL=https://your-explorer-endpoint/v1
#     export OPENROUTER_API_KEY=your-explorer-api-key

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTO_EXPLORE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${AUTO_EXPLORE_ROOT}/.." && pwd)"

APP_NAME="${AUTO_EXPLORE_APP_NAME:-DemoApp}"
DEPTH="${AUTO_EXPLORE_DEPTH:-2}"
BREADTH="${AUTO_EXPLORE_BREADTH:-2}"

# MobileWorld task names used to initialize simulator snapshots.
TASK_NAMES=(
  "SearchItemAndCheckoutTask"
)

SIMULATOR_FILE="${AUTO_EXPLORE_SIMULATOR_FILE:-${AUTO_EXPLORE_ROOT}/configs/simulators.json}"

# Extra simulator specs use: name|backend_url|adb_endpoint[|init_device]
EXTRA_SIMULATORS=()

DEVICE="${AUTO_EXPLORE_DEVICE:-Android}"
DECIDER_BASE_URL="${AUTO_EXPLORE_DECIDER_BASE_URL:-}"
DECIDER_MODEL="${AUTO_EXPLORE_DECIDER_MODEL:-MobiMind-1.5-4B}"
DECIDER_API_KEY="${DECIDER_API_KEY:-}"

EXPLORER_MODEL="${AUTO_EXPLORE_EXPLORER_MODEL:-qwen/qwen3-vl-235b-a22b-instruct}"
OPENROUTER_BASE_URL="${AUTO_EXPLORE_EXPLORER_BASE_URL:-https://openrouter.ai/api/v1}"
OPENROUTER_API_KEY="${AUTO_EXPLORE_EXPLORER_API_KEY:-${OPENROUTER_API_KEY:-}}"
: "${OPENROUTER_API_KEY:?Please export OPENROUTER_API_KEY or AUTO_EXPLORE_EXPLORER_API_KEY first}"

USE_QWEN3="${AUTO_EXPLORE_USE_QWEN3:-on}"
ALLOW_HIERARCHY_TEXT_DECIDER="${AUTO_EXPLORE_ALLOW_HIERARCHY_TEXT_DECIDER:-off}"
ENABLE_UI_SEMANTIC_COLLECT="${AUTO_EXPLORE_ENABLE_UI_SEMANTIC_COLLECT:-on}"

BBOX_IOU_THRESHOLD="${AUTO_EXPLORE_BBOX_IOU_THRESHOLD:-0.1}"
BBOX_CENTER_DIST_RATIO="${AUTO_EXPLORE_BBOX_CENTER_DIST_RATIO:-0.15}"
BBOX_AREA_RATIO_MIN="${AUTO_EXPLORE_BBOX_AREA_RATIO_MIN:-0.3}"
BBOX_AREA_RATIO_MAX="${AUTO_EXPLORE_BBOX_AREA_RATIO_MAX:-3.0}"
POPUP_DISMISS_MAX_ATTEMPTS="${AUTO_EXPLORE_POPUP_DISMISS_MAX_ATTEMPTS:-2}"

UI_COLLECT_ASYNC="${AUTO_EXPLORE_UI_COLLECT_ASYNC:-on}"
UI_COLLECT_QUEUE_SIZE="${AUTO_EXPLORE_UI_COLLECT_QUEUE_SIZE:-8}"
UI_COLLECT_DRAIN_ON_EXIT="${AUTO_EXPLORE_UI_COLLECT_DRAIN_ON_EXIT:-on}"
UI_COLLECT_DRAIN_TIMEOUT_SEC="${AUTO_EXPLORE_UI_COLLECT_DRAIN_TIMEOUT_SEC:-180}"
UI_COLLECT_USE_VLM="${AUTO_EXPLORE_UI_COLLECT_USE_VLM:-on}"
UI_COLLECT_VLM_TEXT_ONLY="${AUTO_EXPLORE_UI_COLLECT_VLM_TEXT_ONLY:-off}"
UI_COLLECT_VLM_MODEL="${AUTO_EXPLORE_UI_COLLECT_VLM_MODEL:-qwen/qwen3-vl-30b-a3b-instruct}"
UI_COLLECT_BASE_URL="${AUTO_EXPLORE_UI_COLLECT_BASE_URL:-${OPENROUTER_BASE_URL}}"
UI_COLLECT_API_KEY="${AUTO_EXPLORE_UI_COLLECT_API_KEY:-${OPENROUTER_API_KEY}}"
UI_COLLECT_MAX_ITEMS="${AUTO_EXPLORE_UI_COLLECT_MAX_ITEMS:-32}"
UI_COLLECT_MAX_VLM_CALLS="${AUTO_EXPLORE_UI_COLLECT_MAX_VLM_CALLS:-12}"
UI_COLLECT_MIN_AREA="${AUTO_EXPLORE_UI_COLLECT_MIN_AREA:-16}"

export PYTHONPATH="${AUTO_EXPLORE_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

cd "${REPO_ROOT}"

CMD=(
  python -m auto_explore.cli.parallel_runner
  --simulator-file "$SIMULATOR_FILE"
  --app_name "$APP_NAME"
  --depth "$DEPTH"
  --breadth "$BREADTH"
  --task-name "${TASK_NAMES[@]}"
  --
  --device "$DEVICE"
  --decider_base_url "$DECIDER_BASE_URL"
  --decider_api_key "$DECIDER_API_KEY"
  --decider_model "$DECIDER_MODEL"
  --openrouter_base_url "$OPENROUTER_BASE_URL"
  --openrouter_api_key "$OPENROUTER_API_KEY"
  --explorer_model "$EXPLORER_MODEL"
  --use_qwen3 "$USE_QWEN3"
  --allow_hierarchy_text_decider "$ALLOW_HIERARCHY_TEXT_DECIDER"
  --enable_ui_semantic_collect "$ENABLE_UI_SEMANTIC_COLLECT"
  --ui_collect_async "$UI_COLLECT_ASYNC"
  --ui_collect_queue_size "$UI_COLLECT_QUEUE_SIZE"
  --ui_collect_drain_on_exit "$UI_COLLECT_DRAIN_ON_EXIT"
  --ui_collect_drain_timeout_sec "$UI_COLLECT_DRAIN_TIMEOUT_SEC"
  --ui_collect_use_vlm "$UI_COLLECT_USE_VLM"
  --ui_collect_vlm_text_only "$UI_COLLECT_VLM_TEXT_ONLY"
  --ui_collect_vlm_model "$UI_COLLECT_VLM_MODEL"
  --ui_collect_base_url "$UI_COLLECT_BASE_URL"
  --ui_collect_api_key "$UI_COLLECT_API_KEY"
  --ui_collect_max_items "$UI_COLLECT_MAX_ITEMS"
  --ui_collect_max_vlm_calls "$UI_COLLECT_MAX_VLM_CALLS"
  --ui_collect_min_area "$UI_COLLECT_MIN_AREA"
  --bbox_iou_threshold "$BBOX_IOU_THRESHOLD"
  --bbox_center_dist_ratio "$BBOX_CENTER_DIST_RATIO"
  --bbox_area_ratio_min "$BBOX_AREA_RATIO_MIN"
  --bbox_area_ratio_max "$BBOX_AREA_RATIO_MAX"
  --popup_dismiss_max_attempts "$POPUP_DISMISS_MAX_ATTEMPTS"
)

for simulator in "${EXTRA_SIMULATORS[@]}"; do
  CMD+=(--simulator "$simulator")
done

echo "Running parallel auto-search for app=$APP_NAME depth=$DEPTH breadth=$BREADTH ..."
"${CMD[@]}"
