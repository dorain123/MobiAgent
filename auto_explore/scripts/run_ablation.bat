@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

rem Auto-search ablation template for Windows.
rem For Chinese app names, prefer setting AUTO_EXPLORE_APP_NAME in the shell before running.
rem Keep the default APP_NAME ASCII-only to avoid Windows batch encoding issues.

set "APP_NAME=DemoApp"
set "DEPTH=8"
set "BREADTH=15"
set "DEVICE=Android"
set "REPEATS=3"

rem Decider configuration.
set "DECIDER_BASE_URL=http://166.111.53.96:7003/v1"
set "DECIDER_MODEL=MobiMind-1.5-4B"

if not defined SJTU_API_KEY (
    echo Error: Please set SJTU_API_KEY environment variable first.
    pause
    exit /b 1
)

set "DECIDER_API_KEY="

rem Explorer configuration.
rem set "EXPLORER_MODEL=qwen3vl"
rem set "OPENROUTER_BASE_URL=https://models.sjtu.edu.cn/api/v1"
rem set "OPENROUTER_API_KEY=%SJTU_API_KEY%"
set "EXPLORER_MODEL=Qwen3.5-35B-A3B"
set "OPENROUTER_BASE_URL=http://166.111.53.96:7002/v1"
set "OPENROUTER_API_KEY="
set "EXPLORER_DISABLE_THINKING=on"

rem Runtime options.
set "USE_QWEN3=on"
set "ALLOW_HIERARCHY_TEXT_DECIDER=on"
set "ENABLE_UI_SEMANTIC_COLLECT=on"

rem BBox refinement options.
set "BBOX_IOU_THRESHOLD=0.1"
set "BBOX_CENTER_DIST_RATIO=0.15"
set "BBOX_AREA_RATIO_MIN=0.3"
set "BBOX_AREA_RATIO_MAX=3.0"

rem Popup handling.
set "POPUP_DISMISS_MAX_ATTEMPTS=2"

rem UI collection options.
set "UI_COLLECT_ASYNC=on"
set "UI_COLLECT_QUEUE_SIZE=8"
set "UI_COLLECT_DRAIN_ON_EXIT=on"
set "UI_COLLECT_DRAIN_TIMEOUT_SEC=180"
set "UI_COLLECT_USE_VLM=on"
set "UI_COLLECT_VLM_TEXT_ONLY=off"
set "UI_COLLECT_VLM_MODEL=qwen/qwen3-vl-30b-a3b-instruct"
set "UI_COLLECT_BASE_URL=https://models.sjtu.edu.cn/api/v1"
set "UI_COLLECT_API_KEY=%SJTU_API_KEY%"
set "UI_COLLECT_MAX_ITEMS=32"
set "UI_COLLECT_MAX_VLM_CALLS=12"
set "UI_COLLECT_MIN_AREA=16"

if defined AUTO_EXPLORE_APP_NAME set "APP_NAME=%AUTO_EXPLORE_APP_NAME%"
if defined AUTO_EXPLORE_ABLATION_REPEATS set "REPEATS=%AUTO_EXPLORE_ABLATION_REPEATS%"
if defined AUTO_EXPLORE_DEPTH set "DEPTH=%AUTO_EXPLORE_DEPTH%"
if defined AUTO_EXPLORE_BREADTH set "BREADTH=%AUTO_EXPLORE_BREADTH%"

if "%APP_NAME%"=="" (
    echo Error: APP_NAME is empty.
    echo Please set AUTO_EXPLORE_APP_NAME before running, for example:
    echo   set AUTO_EXPLORE_APP_NAME=Taobao
    pause
    exit /b 1
)

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "AUTO_EXPLORE_DIR=%%~fI"
set "SRC_ROOT=%AUTO_EXPLORE_DIR%\src"
for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "$ts = Get-Date -Format 'yyyyMMdd_HHmmss'; $name = $env:APP_NAME -replace '[\\/:*?""<>| ]', '_'; Write-Output ($ts + '_' + $name)"`) do set "RUN_DIR_SUFFIX=%%I"
set "OUTPUT_ROOT=%AUTO_EXPLORE_DIR%\results\ablation\%RUN_DIR_SUFFIX%"

if defined AUTO_EXPLORE_ABLATION_OUTPUT_ROOT set "OUTPUT_ROOT=%AUTO_EXPLORE_ABLATION_OUTPUT_ROOT%"

if exist "C:\Users\28125\anaconda3\envs\MobiAgent\python.exe" (
    set "PYTHON_EXE=C:\Users\28125\anaconda3\envs\MobiAgent\python.exe"
) else (
    set "PYTHON_EXE=python"
)

if "%PYTHONPATH%"=="" (
    set "PYTHONPATH=%SRC_ROOT%"
) else (
    set "PYTHONPATH=%SRC_ROOT%;%PYTHONPATH%"
)

echo Running ablation with app=%APP_NAME% depth=%DEPTH% breadth=%BREADTH% repeats=%REPEATS% ...
echo Output root: %OUTPUT_ROOT%

set CMD="%PYTHON_EXE%" -m auto_explore.cli.ablation_runner ^
 --output-root "%OUTPUT_ROOT%" ^
 --repeats "%REPEATS%" ^
 -- ^
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
 --explorer_disable_thinking "%EXPLORER_DISABLE_THINKING%" ^
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

pushd "%SRC_ROOT%"
%CMD%
set "EXIT_CODE=%ERRORLEVEL%"
popd

pause
exit /b %EXIT_CODE%
