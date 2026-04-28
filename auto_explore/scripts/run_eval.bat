@echo off
chcp 65001 >nul
setlocal

rem Auto-search trajectory evaluation template for Windows.
rem Override settings with AUTO_EXPLORE_EVAL_* environment variables when needed.

set "TARGET_LEVEL=auto"
set "JUDGE_MODE=legacy_text"
set "JUDGE_BASE_URL=http://166.111.53.96:7002/v1"
set "JUDGE_MODEL=Qwen3.5-35B-A3B"
set "OUTPUT_PATH="
set "MAX_SAMPLES=0"
set "MAX_TOKENS=8192"
set "ENABLE_THINKING=on"
set "CONTINUE_ON_ERROR=on"

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "AUTO_EXPLORE_DIR=%%~fI"
set "SRC_ROOT=%AUTO_EXPLORE_DIR%\src"
set "INPUT_PATH="

if defined AUTO_EXPLORE_EVAL_INPUT_PATH set "INPUT_PATH=%AUTO_EXPLORE_EVAL_INPUT_PATH%"
if defined AUTO_EXPLORE_EVAL_TARGET_LEVEL set "TARGET_LEVEL=%AUTO_EXPLORE_EVAL_TARGET_LEVEL%"
if defined AUTO_EXPLORE_EVAL_JUDGE_MODE set "JUDGE_MODE=%AUTO_EXPLORE_EVAL_JUDGE_MODE%"
if defined AUTO_EXPLORE_EVAL_BASE_URL set "JUDGE_BASE_URL=%AUTO_EXPLORE_EVAL_BASE_URL%"
if defined AUTO_EXPLORE_EVAL_MODEL set "JUDGE_MODEL=%AUTO_EXPLORE_EVAL_MODEL%"
if defined AUTO_EXPLORE_EVAL_OUTPUT_PATH set "OUTPUT_PATH=%AUTO_EXPLORE_EVAL_OUTPUT_PATH%"
if defined AUTO_EXPLORE_EVAL_MAX_SAMPLES set "MAX_SAMPLES=%AUTO_EXPLORE_EVAL_MAX_SAMPLES%"
if defined AUTO_EXPLORE_EVAL_MAX_TOKENS set "MAX_TOKENS=%AUTO_EXPLORE_EVAL_MAX_TOKENS%"
if defined AUTO_EXPLORE_EVAL_ENABLE_THINKING set "ENABLE_THINKING=%AUTO_EXPLORE_EVAL_ENABLE_THINKING%"
if defined AUTO_EXPLORE_EVAL_CONTINUE_ON_ERROR set "CONTINUE_ON_ERROR=%AUTO_EXPLORE_EVAL_CONTINUE_ON_ERROR%"

set "JUDGE_API_KEY=mobiagent-key"
if defined AUTO_EXPLORE_EVAL_API_KEY set "JUDGE_API_KEY=%AUTO_EXPLORE_EVAL_API_KEY%"
if not "%JUDGE_BASE_URL:http://166.111.53.96:7002/v1=%"=="%JUDGE_BASE_URL%" set "JUDGE_API_KEY=mobiagent-key"
if not "%JUDGE_BASE_URL:http://166.111.53.96:7002/v1=%"=="%JUDGE_BASE_URL%" goto judge_key_ready
if "%AUTO_EXPLORE_EVAL_API_KEY%"=="" if defined SJTU_API_KEY set "JUDGE_API_KEY=%SJTU_API_KEY%"
if "%AUTO_EXPLORE_EVAL_API_KEY%"=="" if defined OPENROUTER_API_KEY set "JUDGE_API_KEY=%OPENROUTER_API_KEY%"
:judge_key_ready

if "%INPUT_PATH%"=="" (
    echo Error: INPUT_PATH is empty.
    echo Please set AUTO_EXPLORE_EVAL_INPUT_PATH before running.
    pause
    exit /b 1
)

if "%JUDGE_MODEL%"=="" (
    echo Error: JUDGE_MODEL is empty.
    echo Please set AUTO_EXPLORE_EVAL_MODEL before running.
    pause
    exit /b 1
)

if "%JUDGE_API_KEY%"=="" (
    echo Error: Please set AUTO_EXPLORE_EVAL_API_KEY, SJTU_API_KEY, or OPENROUTER_API_KEY first.
    pause
    exit /b 1
)

if exist "C:\Users\28125\anaconda3\envs\collect\python.exe" (
    set "PYTHON_EXE=C:\Users\28125\anaconda3\envs\collect\python.exe"
) else if exist "C:\Users\28125\anaconda3\envs\MobiAgent\python.exe" (
    set "PYTHON_EXE=C:\Users\28125\anaconda3\envs\MobiAgent\python.exe"
) else (
    set "PYTHON_EXE=python"
)

if "%PYTHONPATH%"=="" (
    set "PYTHONPATH=%SRC_ROOT%"
) else (
    set "PYTHONPATH=%SRC_ROOT%;%PYTHONPATH%"
)

echo Running auto-search eval...
echo Input path: %INPUT_PATH%
echo Target level: %TARGET_LEVEL%
echo Judge mode: %JUDGE_MODE%
echo Judge base URL: %JUDGE_BASE_URL%
echo Judge model: %JUDGE_MODEL%
echo Judge thinking: %ENABLE_THINKING%
echo Judge max tokens: %MAX_TOKENS%
if not "%OUTPUT_PATH%"=="" echo Output path: %OUTPUT_PATH%

set CMD="%PYTHON_EXE%" -m auto_explore.eval.cli ^
 --input_path "%INPUT_PATH%" ^
 --target_level "%TARGET_LEVEL%" ^
 --judge_mode "%JUDGE_MODE%" ^
 --judge_base_url "%JUDGE_BASE_URL%" ^
 --judge_api_key "%JUDGE_API_KEY%" ^
 --judge_model "%JUDGE_MODEL%" ^
 --max_samples "%MAX_SAMPLES%" ^
 --max_tokens "%MAX_TOKENS%" ^
 --enable_thinking "%ENABLE_THINKING%" ^
 --continue_on_error "%CONTINUE_ON_ERROR%"

if not "%OUTPUT_PATH%"=="" set CMD=%CMD% --output_path "%OUTPUT_PATH%"

pushd "%SRC_ROOT%"
%CMD%
set "EXIT_CODE=%ERRORLEVEL%"
popd

if not "%EXIT_CODE%"=="0" (
    echo Evaluation failed with exit code %EXIT_CODE%.
    pause
    exit /b %EXIT_CODE%
)

pause
