@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

:: ==========================================================
:: Auto Search 运行参数模板 (Windows .bat 版本)
:: 使用方式：直接双击运行，或在 CMD/PowerShell 中执行此文件
:: ==========================================================

:: 基础参数配置（Decider 使用 DECIDER_BASE_URL）
set APP_NAME=美团
set DEPTH=2
set BREADTH=10
set DEVICE=Android
set DECIDER_BASE_URL=http://166.111.53.96:7003
set DECIDER_MODEL=MobiMind-1.5-4B
:: Decider API Key：通过环境变量 DECIDER_API_KEY 传入，默认为 mobiagent-key
if not defined DECIDER_API_KEY set DECIDER_API_KEY=mobiagent-key

:: Explorer 相关参数（使用 SJTU 服务）
set EXPLORER_MODEL=qwen3vl
set OPENROUTER_BASE_URL=https://models.sjtu.edu.cn/api/v1
:: Explorer API Key：通过环境变量 SJTU_API_KEY 传入（必需）
if not defined SJTU_API_KEY (
    echo Error: Please set SJTU_API_KEY environment variable first
    pause
    exit /b 1
)
set OPENROUTER_API_KEY=%SJTU_API_KEY%

:: 运行模式配置
set USE_QWEN3=on
set DATA_DIR=
set ALLOW_HIERARCHY_TEXT_DECIDER=off
set ENABLE_UI_SEMANTIC_COLLECT=on

:: BBox 精炼阈值（换模型/换手机时调整）
:: BBOX_IOU_THRESHOLD:      IoU >= 此值则用 XML 元素边框（模型越不准确 → 调低，如 0.1）
:: BBOX_CENTER_DIST_RATIO:  中心距/对角线 <= 此值才匹配（偏差大 → 调高，如 0.15）
:: BBOX_AREA_RATIO_MIN/MAX: 候选元素面积/模型bbox面积的允许范围
set BBOX_IOU_THRESHOLD=0.1
set BBOX_CENTER_DIST_RATIO=0.15
set BBOX_AREA_RATIO_MIN=0.3
set BBOX_AREA_RATIO_MAX=3.0

:: 弹窗自动关闭（Explorer VLM 检测到广告/弹窗时自动点关闭按钮）
:: POPUP_DISMISS_MAX_ATTEMPTS: 最多尝试几次（0 表示禁用，建议 2）
set POPUP_DISMISS_MAX_ATTEMPTS=2

:: UI 采集模块配置
set UI_COLLECT_ASYNC=on
set UI_COLLECT_QUEUE_SIZE=8
set UI_COLLECT_DRAIN_ON_EXIT=on
set UI_COLLECT_DRAIN_TIMEOUT_SEC=180
set UI_COLLECT_USE_VLM=on
set UI_COLLECT_VLM_TEXT_ONLY=off
set UI_COLLECT_VLM_MODEL=qwen/qwen3-vl-30b-a3b-instruct
set UI_COLLECT_BASE_URL=%OPENROUTER_BASE_URL%
set UI_COLLECT_API_KEY=%OPENROUTER_API_KEY%
set UI_COLLECT_MAX_ITEMS=32
set UI_COLLECT_MAX_VLM_CALLS=12
set UI_COLLECT_MIN_AREA=16
set PYTHONPATH=%CD%\auto_explore\src;%PYTHONPATH%

echo Running auto-search with app=%APP_NAME% depth=%DEPTH% breadth=%BREADTH% ...

:: 构建命令字符串
set CMD=python -m auto_explore.cli.auto_search ^
 --app_name "%APP_NAME%" ^
 --depth "%DEPTH%" ^
 --breadth "%BREADTH%" ^
 --device "%DEVICE%" ^
 --decider_base_url "%DECIDER_BASE_URL%" ^
 --decider_api_key "%DECIDER_API_KEY%" ^
 --decider_model "%DECIDER_MODEL%" ^
 --openrouter_base_url "%OPENROUTER_BASE_URL%" ^
 --openrouter_api_key "%OPENROUTER_API_KEY%" ^
 --explorer_model "%EXPLORER_MODEL%" ^
 --use_qwen3 "%USE_QWEN3%" ^
 --allow_hierarchy_text_decider "%ALLOW_HIERARCHY_TEXT_DECIDER%" ^
 --enable_ui_semantic_collect "%ENABLE_UI_SEMANTIC_COLLECT%" ^
 --ui_collect_async "%UI_COLLECT_ASYNC%" ^
 --ui_collect_queue_size "%UI_COLLECT_QUEUE_SIZE%" ^
 --ui_collect_drain_on_exit "%UI_COLLECT_DRAIN_ON_EXIT%" ^
 --ui_collect_drain_timeout_sec "%UI_COLLECT_DRAIN_TIMEOUT_SEC%" ^
 --ui_collect_use_vlm "%UI_COLLECT_USE_VLM%" ^
 --ui_collect_vlm_text_only "%UI_COLLECT_VLM_TEXT_ONLY%" ^
 --ui_collect_vlm_model "%UI_COLLECT_VLM_MODEL%" ^
 --ui_collect_base_url "%UI_COLLECT_BASE_URL%" ^
 --ui_collect_api_key "%UI_COLLECT_API_KEY%" ^
 --ui_collect_max_items "%UI_COLLECT_MAX_ITEMS%" ^
 --ui_collect_max_vlm_calls "%UI_COLLECT_MAX_VLM_CALLS%" ^
 --ui_collect_min_area "%UI_COLLECT_MIN_AREA%" ^
 --bbox_iou_threshold "%BBOX_IOU_THRESHOLD%" ^
 --bbox_center_dist_ratio "%BBOX_CENTER_DIST_RATIO%" ^
 --bbox_area_ratio_min "%BBOX_AREA_RATIO_MIN%" ^
 --bbox_area_ratio_max "%BBOX_AREA_RATIO_MAX%" ^
 --popup_dismiss_max_attempts "%POPUP_DISMISS_MAX_ATTEMPTS%"

:: 如果 DATA_DIR 不为空，则添加参数
if not "%DATA_DIR%"=="" set CMD=%CMD% --data_dir "%DATA_DIR%"

:: 执行命令
%CMD%

pause
