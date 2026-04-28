#!/usr/bin/env bash
set -euo pipefail

# Auto Search 运行参数模板
# 使用方式：
# 1) 修改下面变量
# 2) 执行: bash auto_explore/scripts/run_single.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTO_EXPLORE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${AUTO_EXPLORE_ROOT}/.." && pwd)"

APP_NAME="淘宝"
DEPTH=2 # 探索页面的深度，3-4
BREADTH=2 # 在每一个页面探索的广度，5-10，路径总数最多为BREADTH的DEPTH次方

# Decider 相关参数（使用 DECIDER_BASE_URL）
DEVICE="Android"                 # Android | Harmony
DECIDER_BASE_URL="http://166.111.53.96:7003/v1"
DECIDER_MODEL="MobiMind-1.5-4B"
# Decider API Key: pass via DECIDER_API_KEY. Default is empty for privacy.
DECIDER_API_KEY="${DECIDER_API_KEY:-}"

# Explorer 相关参数（使用 OpenRouter 服务）
EXPLORER_MODEL="qwen/qwen3-vl-235b-a22b-instruct"
OPENROUTER_BASE_URL="https://openrouter.ai/api/v1"
# Explorer API Key：通过环境变量 OPENROUTER_API_KEY 传入（必需）
: "${OPENROUTER_API_KEY:?Please export OPENROUTER_API_KEY first}"
USE_QWEN3="on"                   # on | off
DATA_DIR=""                      # 为空时使用默认输出目录
ALLOW_HIERARCHY_TEXT_DECIDER="off"
ENABLE_UI_SEMANTIC_COLLECT="on"   # on | off, 是否在auto-search过程中启用UI语义信息收集模块
                                  #提供给VLM更丰富的页面信息以辅助决策；开启后会增加一定的API调用和整体运行时间，请根据需要选择是否开启

# BBox 精炼阈值（换模型/换手机时调整）
# BBOX_IOU_THRESHOLD:      IoU >= 此值则用 XML 元素边框（模型越不准确 → 调低，如 0.1）
# BBOX_CENTER_DIST_RATIO:  中心距/对角线 <= 此值才匹配（偏差大 → 调高，如 0.15）
# BBOX_AREA_RATIO_MIN/MAX: 候选元素面积/模型bbox面积的允许范围
BBOX_IOU_THRESHOLD=0.1
BBOX_CENTER_DIST_RATIO=0.15
BBOX_AREA_RATIO_MIN=0.3
BBOX_AREA_RATIO_MAX=3.0

# 弹窗自动关闭（Explorer VLM 检测到广告/弹窗时自动点关闭按钮）
# POPUP_DISMISS_MAX_ATTEMPTS: 最多尝试几次（0 表示禁用，建议 2）
POPUP_DISMISS_MAX_ATTEMPTS=2

UI_COLLECT_ASYNC="on"             # on | off
UI_COLLECT_QUEUE_SIZE=8
UI_COLLECT_DRAIN_ON_EXIT="on"     # on | off
UI_COLLECT_DRAIN_TIMEOUT_SEC=180
UI_COLLECT_USE_VLM="on"           # on | off
UI_COLLECT_VLM_TEXT_ONLY="off"    # on | off，当页面中存在歧义的元素或者需要更准确的对象描述时，开启；只使用VLM提供的文本，层级信息仍然来自页面结构
UI_COLLECT_VLM_MODEL="qwen/qwen3-vl-30b-a3b-instruct" # qwen/qwen3.5-35b-a3b
UI_COLLECT_BASE_URL="${OPENROUTER_BASE_URL}"  # 可单独指定UI采集模型提供商，例如本地vLLM: http://127.0.0.1:8001/v1
UI_COLLECT_API_KEY="${OPENROUTER_API_KEY}"    # 可单独指定UI采集key；本地vLLM通常可留空
UI_COLLECT_MAX_ITEMS=32
UI_COLLECT_MAX_VLM_CALLS=12
UI_COLLECT_MIN_AREA=16


export PYTHONPATH="${AUTO_EXPLORE_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

cd "${REPO_ROOT}"

CMD=(
  python -m auto_explore.cli.auto_search
  --app_name "$APP_NAME"
  --depth "$DEPTH"
  --breadth "$BREADTH"
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

if [[ -n "$DATA_DIR" ]]; then
  CMD+=(--data_dir "$DATA_DIR")
fi

echo "Running auto-search with app=$APP_NAME depth=$DEPTH breadth=$BREADTH ..."
"${CMD[@]}"
