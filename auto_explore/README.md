# Auto Explore

`auto_explore/` 是 MobiAgent 的自动化轨迹采集与评估模块。它用 Explorer 模型为当前移动端页面生成候选单步任务，再由 Decider 模型执行具体点击、输入、滑动等动作，并通过 DFS 搜索、多策略回溯、轨迹保存和离线评估来构建可复用的移动 GUI 操作数据。

核心目标：

- 快速在真实 Android/Harmony 设备或模拟器上采集多步操作轨迹。
- 用 Explorer + Decider 解耦“候选生成”和“动作执行”。
- 用语义回溯、指纹校验和全路径重播保证 DFS 分支从正确父状态继续。
- 保存 `steps/`、`paths/`、`partial_paths/`、`ui-pages/` 和 `metrics.json`，便于训练、分析和评估。

## 目录结构

```text
auto_explore/
├── README.md
├── configs/
│   └── simulators.example.json       # 并行模拟器配置示例
├── data/                             # 默认 auto_search 输出目录
├── eval/
│   └── results/                      # 评估结果
├── results/                          # 脚本默认输出目录
├── scripts/
│   ├── run_single.bat                # Windows 单机运行模板
│   ├── run_single.sh                 # Linux/macOS 单机运行模板
│   ├── run_parallel.sh               # 并行运行模板
│   ├── run_eval.bat                  # Windows 评估模板
│   ├── run_ablation.bat              # Windows 消融实验模板
│   └── snapshot_manager.py           # MobileWorld 快照初始化客户端
├── src/auto_explore/
│   ├── cli/
│   │   ├── auto_search.py            # 单机 DFS 采集入口
│   │   ├── parallel_runner.py        # 多模拟器并行入口
│   │   └── ablation_runner.py        # 消融实验入口
│   ├── core/
│   │   ├── dfs.py                    # DFS 主循环、候选过滤、回溯恢复
│   │   ├── navigation.py             # Back、反向滑动、语义回溯、重播
│   │   ├── fingerprints.py           # 文本/结构/视觉指纹与回溯校验
│   │   ├── explorer.py               # 页面采集、Explorer 调用、缓存
│   │   ├── decider.py                # Decider 调用、动作执行、目标校验
│   │   ├── ui_collect.py             # UI 语义采集任务
│   │   └── runtime.py                # feature flags 与 metrics
│   └── eval/
│       └── cli.py                    # 轨迹评估入口
└── tests/                            # 单元测试与回归测试
```

## 环境准备

建议在项目根目录运行命令：

```powershell
cd D:\cdl\MobiAgent
conda activate collect
$env:PYTHONPATH = "$PWD\auto_explore\src;$env:PYTHONPATH"
```

Linux/macOS：

```bash
cd /path/to/MobiAgent
conda activate collect
export PYTHONPATH="${PWD}/auto_explore/src${PYTHONPATH:+:${PYTHONPATH}}"
```

常用环境变量：

| 变量 | 说明 |
|------|------|
| `SJTU_API_KEY` | 上海交大模型服务 API Key，脚本默认会优先使用 |
| `OPENROUTER_API_KEY` | Explorer 或评估模型使用 OpenRouter 时的 API Key |
| `DECIDER_API_KEY` | Decider 服务需要鉴权时使用；本地兼容服务可为空 |
| `AUTO_EXPLORE_APP_NAME` | Windows `run_single.bat` 中覆盖默认 App 名称 |
| `AUTO_EXPLORE_EVAL_*` | Windows `run_eval.bat` 中覆盖评估配置 |

设备要求：

- Android：需要 ADB 可连接，真机或模拟器均可。远程模拟器可通过 `--adb_endpoint host:port` 指定。
- Harmony：使用 `--device Harmony`，底层由 `HarmonyDevice` 适配。
- 目标 App 名称需要能被当前设备适配器识别并启动；如果存在包名映射问题，优先检查设备适配层和日志中的 foreground app mismatch。

## 单机 DFS 采集

Windows 推荐从模板脚本开始：

```bat
set OPENROUTER_API_KEY=your-key
set AUTO_EXPLORE_EXPLORER_BASE_URL=https://your-openai-compatible-endpoint/v1
set AUTO_EXPLORE_APP_NAME=Taobao
set AUTO_EXPLORE_DEPTH=2
set AUTO_EXPLORE_BREADTH=2
auto_explore\scripts\run_single.bat
```

也可以直接运行 CLI：

```powershell
$env:PYTHONPATH = "$PWD\auto_explore\src;$env:PYTHONPATH"
$env:OPENROUTER_API_KEY = "your-key"
$env:AUTO_EXPLORE_EXPLORER_BASE_URL = "https://your-openai-compatible-endpoint/v1"
$env:AUTO_EXPLORE_DECIDER_BASE_URL = "https://your-decider-endpoint/v1"

python -m auto_explore.cli.auto_search `
  --app_name "Taobao" `
  --depth 2 `
  --breadth 2 `
  --device Android `
  --decider_base_url "$env:AUTO_EXPLORE_DECIDER_BASE_URL" `
  --decider_api_key "" `
  --decider_model "MobiMind-1.5-4B" `
  --openrouter_base_url "$env:AUTO_EXPLORE_EXPLORER_BASE_URL" `
  --openrouter_api_key "$env:OPENROUTER_API_KEY" `
  --explorer_model "qwen3vl" `
  --use_qwen3 on `
  --allow_hierarchy_text_decider on `
  --enable_ui_semantic_collect on `
  --ui_collect_async on `
  --popup_dismiss_max_attempts 2
```

Linux/macOS：

```bash
export PYTHONPATH="${PWD}/auto_explore/src${PYTHONPATH:+:${PYTHONPATH}}"
export OPENROUTER_API_KEY="your-key"
export AUTO_EXPLORE_EXPLORER_BASE_URL="https://your-openai-compatible-endpoint/v1"
export AUTO_EXPLORE_DECIDER_BASE_URL="https://your-decider-endpoint/v1"

python -m auto_explore.cli.auto_search \
  --app_name "Taobao" \
  --depth 2 \
  --breadth 2 \
  --device Android \
  --decider_base_url "$AUTO_EXPLORE_DECIDER_BASE_URL" \
  --decider_api_key "" \
  --decider_model "MobiMind-1.5-4B" \
  --openrouter_base_url "$AUTO_EXPLORE_EXPLORER_BASE_URL" \
  --openrouter_api_key "$OPENROUTER_API_KEY" \
  --explorer_model "qwen3vl" \
  --use_qwen3 on \
  --allow_hierarchy_text_decider on \
  --enable_ui_semantic_collect on \
  --ui_collect_async on \
  --popup_dismiss_max_attempts 2
```

### 常用参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--app_name` | 必填 | 要启动和探索的 App 名称 |
| `--depth` | 必填 | DFS 最大深度 |
| `--breadth` | 必填 | 每页最多执行的候选数 |
| `--device` | `Android` | `Android` 或 `Harmony` |
| `--adb_endpoint` | 空 | Android 远程 ADB 地址，例如 `127.0.0.1:5555` |
| `--data_dir` | 自动生成 | 输出目录；不传时写入 `auto_explore/data/<app>/<timestamp>/` |
| `--metrics_output_path` | `data_dir/metrics.json` | metrics 输出路径 |
| `--experiment_tag` | `default` | 写入 metrics 的实验标签 |

模型参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--decider_base_url` | 空 | Decider OpenAI-compatible 服务地址；为空时使用 `--service_ip` + `--decider_port` |
| `--decider_api_key` | `DECIDER_API_KEY` | Decider API Key |
| `--decider_model` | 内置占位值 | Decider 模型名 |
| `--openrouter_base_url` | `https://openrouter.ai/api/v1` | Explorer OpenAI-compatible 服务地址 |
| `--openrouter_api_key` | `OPENROUTER_API_KEY` | Explorer API Key |
| `--explorer_model` | `google/gemini-3-flash-preview` | Explorer 模型名 |
| `--explorer_disable_thinking` | `off` | 对支持 thinking 的模型关闭推理模式 |

执行与页面稳定参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--use_qwen3` | `on` | 是否使用 Qwen3 坐标转换 |
| `--allow_hierarchy_text_decider` | `on` | 是否允许 Decider 前置使用 hierarchy 文本定位 |
| `--page_load_wait_sec` | `1.5` | 动作后固定等待时间 |
| `--page_load_stable_max_polls` | `6` | 页面稳定轮询次数 |
| `--popup_dismiss_max_attempts` | `2` | 自动关闭弹窗的最大尝试次数 |

BBox refine 参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--bbox_iou_threshold` | `0.3` | 候选框 IoU 阈值 |
| `--bbox_center_dist_ratio` | `0.08` | 中心距离阈值比例 |
| `--bbox_area_ratio_min` | `0.5` | 面积比例下限 |
| `--bbox_area_ratio_max` | `2.0` | 面积比例上限 |

UI 语义采集参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--enable_ui_semantic_collect` | `off` | 是否采集页面 UI 语义数据 |
| `--ui_collect_async` | `on` | 是否异步采集 |
| `--ui_collect_queue_size` | `256` | 异步队列大小 |
| `--ui_collect_num_workers` | `1` | 采集 worker 数量 |
| `--ui_collect_drain_on_exit` | `on` | 退出前是否等待队列清空 |
| `--ui_collect_drain_timeout_sec` | `180` | drain 超时时间 |
| `--ui_collect_use_vlm` | `on` | UI 采集是否调用 VLM |
| `--ui_collect_vlm_text_only` | `off` | 是否只使用 VLM 文本结果 |
| `--ui_collect_vlm_model` | `qwen/qwen3-vl-30b-a3b-instruct` | UI 采集 VLM 模型 |
| `--ui_collect_base_url` | Explorer base URL | UI 采集 VLM 服务地址 |
| `--ui_collect_api_key` | Explorer API Key | UI 采集 VLM API Key |
| `--ui_collect_max_items` | `32` | 每页最多采集元素数 |
| `--ui_collect_max_vlm_calls` | `12` | 每页最多 VLM 调用数 |
| `--ui_collect_min_area` | `16` | UI 元素最小面积 |

运行时 feature flags 默认全部开启，可用 `on/off` 控制：

```text
--enable_triple_verify
--enable_replay_recovery
--enable_candidate_dedup
--enable_already_explored_filter
--enable_explorer_cache
--enable_screen_cache
--enable_popup_auto_dismiss
--enable_async_artifact_io
--enable_concurrent_fingerprint
--enable_hierarchy_text_decider
```

## DFS 与回溯机制

Auto Explore 的主循环在 `core/dfs.py`：

1. 采集当前页面截图与 hierarchy。
2. Explorer 根据当前页面生成候选单步任务。
3. 候选去重、可见性过滤、已探索过滤。
4. Decider 执行候选动作，得到 `action_record` 和 `react_item`。
5. 根据前后 hierarchy、结构指纹、视觉 dHash 判断是否产生有效进展。
6. 如果有进展，递归进入下一层 DFS；如果到达深度上限，保存完整 path。
7. 当前分支结束后执行回溯，回到父页面后继续兄弟候选。

当前回溯优先使用语义逆操作：

- `input` / `click_input`：先按 Back，退出输入态或关闭键盘。
- `swipe`：根据原滑动方向执行反向滑动。
- tab/navigation 类 `click`：点击执行前记录的 `selected=true` tab，回到原导航项。
- 其他动作：退化为系统 Back。

tab 回溯会在动作执行前记录 `pre_selected_tab`。如果页面同时有顶部频道 tab 和底部导航栏，代码会选择与本次点击区域 y 坐标最近的 selected tab，避免把顶部 tab 和底部导航混在一起。

回溯后会进行校验：

- relaxed verify：优先比较稳定 UI 文本集合，例如标题栏、导航栏、选中 tab、固定控件；忽略动态列表、时间戳、未读点、feed 内容和图片。
- triple verify：文本指纹、结构指纹、视觉 dHash 的组合校验。
- replay recovery：如果普通回溯验证失败，会重启 App，并重放当前路径中最后一步之前的动作，最多尝试 2 次。

如果最终仍无法验证，但 App 仍在前台且 hierarchy 可读，DFS 会继续尝试未执行的兄弟候选；如果 App 不可恢复，则跳过当前深度剩余候选。

## 输出结构

单机默认输出：

```text
auto_explore/data/<app_name>/<timestamp>/
├── steps/
│   └── step_*/
├── paths/
│   └── path_*/
├── partial_paths/
│   └── path_*/
├── ui-pages/
│   ├── pages_index.json
│   └── page_*/
└── metrics.json
```

脚本 `run_single.bat` 默认写入：

```text
auto_explore/results/single/<timestamp>_<app_name>/
```

并行或消融实验还会生成每个 run 的 `runner.log`。

| 路径 | 说明 |
|------|------|
| `steps/` | 每个 Decider 步骤的截图、hierarchy、action、react |
| `paths/` | 达到深度上限并追加 done 的完整轨迹 |
| `partial_paths/` | 无进展、输入失败或分支异常时保存的部分轨迹 |
| `ui-pages/` | 去重后的 UI 页面语义采集结果 |
| `metrics.json` | 运行耗时、模型调用次数、缓存命中率、回溯成功率等指标 |
| `runner.log` | 脚本子进程日志，主要出现在并行/消融输出目录 |

`metrics.json` 常用字段：

| 字段 | 说明 |
|------|------|
| `step_count` | 执行动作步数 |
| `explorer_call_count` | Explorer 调用次数 |
| `decider_call_count` | Decider 调用次数 |
| `backtrack_count` | 回溯次数 |
| `backtrack_verify_success_rate` | 不依赖 replay recovery 的直接验证成功率 |
| `backtrack_recovery_success_rate` | 触发 replay recovery 后的恢复成功率 |
| `final_backtrack_success_rate` | 直接验证成功 + replay recovery 成功的总成功率 |
| `explorer_cache_hit_rate` | Explorer 响应缓存命中率 |
| `screen_cache_hit_rate` | 屏幕状态缓存命中率 |
| `partial_path_count` | 保存的部分轨迹数量 |
| `complete_path_count` | 保存的完整轨迹数量 |

## 多模拟器并行运行

并行入口是 `auto_explore.cli.parallel_runner`。它会先通过 MobileWorld backend 初始化每个模拟器的任务快照，然后为每个模拟器启动一个 `auto_search` 子进程。

配置文件示例：

```json
{
  "simulators": [
    {
      "name": "sim_01",
      "backend_url": "http://127.0.0.1:9000",
      "adb_endpoint": "127.0.0.1:8000",
      "init_device": "emulator-5554"
    },
    {
      "name": "sim_02",
      "backend_url": "http://127.0.0.1:9001",
      "adb_endpoint": "127.0.0.1:8080",
      "init_device": "emulator-5554"
    }
  ]
}
```

字段说明：

| 字段 | 说明 |
|------|------|
| `name` | 模拟器名称，也会作为输出子目录名 |
| `backend_url` | MobileWorld backend 地址 |
| `adb_endpoint` | 当前模拟器暴露的 ADB 地址 |
| `init_device` | backend 初始化快照时使用的设备 ID，默认 `emulator-5554` |

运行示例：

```powershell
$env:PYTHONPATH = "$PWD\auto_explore\src;$env:PYTHONPATH"
$env:OPENROUTER_API_KEY = "your-key"
$env:AUTO_EXPLORE_EXPLORER_BASE_URL = "https://your-openai-compatible-endpoint/v1"
$env:AUTO_EXPLORE_DECIDER_BASE_URL = "https://your-decider-endpoint/v1"

python -m auto_explore.cli.parallel_runner `
  --simulator-file auto_explore/configs/simulators.example.json `
  --task-name MattermostCreateChannel GmailSendEmail `
  --app_name DemoApp `
  --depth 2 `
  --breadth 2 `
  --output-root auto_explore/results/parallel/demo `
  -- `
  --device Android `
  --decider_base_url "$env:AUTO_EXPLORE_DECIDER_BASE_URL" `
  --decider_model "MobiMind-1.5-4B" `
  --openrouter_base_url "$env:AUTO_EXPLORE_EXPLORER_BASE_URL" `
  --openrouter_api_key "$env:OPENROUTER_API_KEY" `
  --explorer_model "qwen3vl"
```

`--` 后面的参数会原样透传给每个 `auto_search` 子进程。

也可以不用 JSON，直接传模拟器：

```bash
python -m auto_explore.cli.parallel_runner \
  --simulator "sim_01|http://127.0.0.1:9000|127.0.0.1:8000|emulator-5554" \
  --task-name MattermostCreateChannel \
  --app_name DemoApp \
  --depth 2 \
  --breadth 2 \
  -- --device Android
```

## 消融实验

消融入口是 `auto_explore.cli.ablation_runner`。它会按实验组多次运行 `auto_search`，每个 run 写出独立 `metrics.json` 和 `runner.log`，最后汇总到 `summary.json` 和 `ablation_metrics.json`。

直接传 `auto_search` 参数：

```powershell
$env:PYTHONPATH = "$PWD\auto_explore\src;$env:PYTHONPATH"

python -m auto_explore.cli.ablation_runner `
  --output-root auto_explore/results/ablation/demo `
  --repeats 1 `
  --experiment E0_full `
  --experiment E2_no_replay_recovery `
  -- `
  --app_name DemoApp `
  --depth 2 `
  --breadth 2 `
  --device Android `
  --openrouter_api_key "$env:OPENROUTER_API_KEY"
```

更推荐用 manifest 管理公共参数。可以新建一个本地文件，例如 `auto_explore/configs/ablation.local.json`：

```json
{
  "app_name": "DemoApp",
  "depth": 2,
  "breadth": 2,
  "extra_args": [
    "--device", "Android",
    "--decider_base_url", "https://your-decider-endpoint/v1",
    "--decider_model", "MobiMind-1.5-4B",
    "--openrouter_base_url", "https://your-openai-compatible-endpoint/v1",
    "--openrouter_api_key", "${OPENROUTER_API_KEY}",
    "--explorer_model", "qwen3vl"
  ]
}
```

```bash
python -m auto_explore.cli.ablation_runner \
  --manifest auto_explore/configs/ablation.local.json \
  --output-root auto_explore/results/ablation/demo \
  --repeats 3
```

默认实验组：

| 实验 | 说明 |
|------|------|
| `E0_full` | 全部默认优化开启 |
| `E1_no_triple_verify` | 关闭三重/宽松回溯验证 |
| `E2_no_replay_recovery` | 关闭全路径重播恢复 |
| `E3_no_candidate_dedup` | 关闭候选去重 |
| `E4_no_already_explored_filter` | 关闭已探索过滤 |
| `E5_no_explorer_cache` | 关闭 Explorer 缓存 |
| `E6_no_screen_cache` | 关闭屏幕状态缓存 |
| `E7_no_popup_auto_dismiss` | 关闭弹窗自动处理 |
| `E8_no_async_artifact_io` | 关闭异步落盘 |
| `E9_no_concurrent_fingerprint` | 关闭并发指纹计算 |
| `E10_no_hierarchy_text_decider` | 关闭 hierarchy 文本辅助 Decider |
| `U0_ui_collect_off` | UI 语义采集关闭 |
| `U1_ui_collect_on` | UI 语义采集开启 |

输出结构：

```text
auto_explore/results/ablation/demo/
├── E0_full/
│   ├── run_001/
│   │   ├── data/
│   │   ├── metrics.json
│   │   └── runner.log
│   └── summary.json
└── ablation_metrics.json
```

## 轨迹评估

评估入口是 `auto_explore.eval.cli`，支持 step/path 级轨迹评估。

Windows 模板：

```bat
set OPENROUTER_API_KEY=your-key
set AUTO_EXPLORE_EVAL_INPUT_PATH=D:\path\to\auto_explore\results\single\demo\paths
set AUTO_EXPLORE_EVAL_BASE_URL=https://your-openai-compatible-endpoint/v1
set AUTO_EXPLORE_EVAL_MODEL=Qwen3.5-35B-A3B
auto_explore\scripts\run_eval.bat
```

直接运行：

```powershell
$env:PYTHONPATH = "$PWD\auto_explore\src;$env:PYTHONPATH"
$env:OPENROUTER_API_KEY = "your-key"
$env:AUTO_EXPLORE_EVAL_BASE_URL = "https://your-openai-compatible-endpoint/v1"

python -m auto_explore.eval.cli `
  --input_path auto_explore/results/single/demo/paths `
  --target_level paths `
  --judge_mode path_multimodal `
  --judge_base_url "$env:AUTO_EXPLORE_EVAL_BASE_URL" `
  --judge_api_key "$env:OPENROUTER_API_KEY" `
  --judge_model "Qwen3.5-35B-A3B" `
  --max_tokens 8192 `
  --enable_thinking on `
  --continue_on_error on
```

参数说明：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--input_path` | 必填 | run 目录、`paths/`、`steps/` 或单个 trace 目录 |
| `--target_level` | `auto` | `auto`、`paths`、`steps` |
| `--judge_mode` | `legacy_text` | `legacy_text` 或 `path_multimodal` |
| `--judge_base_url` | 自动推断 | Judge OpenAI-compatible 服务地址 |
| `--judge_api_key` | 自动推断 | `AUTO_EXPLORE_EVAL_API_KEY`、`SJTU_API_KEY` 或 `OPENROUTER_API_KEY` |
| `--judge_model` | 空 | Judge 模型名，必须提供 |
| `--max_samples` | `0` | 最大评估样本数，`0` 表示不限制 |
| `--max_tokens` | `700` | 每次 judge 调用最大输出 token |
| `--enable_thinking` | `on` | 是否开启 Qwen thinking |
| `--continue_on_error` | `on` | 单条失败后是否继续 |
| `--output_path` | 自动生成 | summary JSON 输出路径 |

`path_multimodal` 是 path-only 模式，只支持 `path_*` 样本，不支持 `steps`。

默认评估结果写入：

```text
auto_explore/eval/results/<timestamp>/summary.json
```

## UI 语义采集

开启 `--enable_ui_semantic_collect on` 后，DFS 每发现一个新页面，会将页面截图和 hierarchy 入队，由 `ui_collect_worker` 调用 `collect.auto.ui_semantic_boxer` 生成 UI 元素语义数据。

输出通常在：

```text
ui-pages/
├── pages_index.json
└── page_000001/
    ├── screenshot.jpg
    ├── hierarchy.json 或 hierarchy.xml
    └── ui_semantics.json
```

建议：

- 调试主流程时可以先关闭：`--enable_ui_semantic_collect off`。
- 需要完整 UI 语义数据时保持 `--ui_collect_drain_on_exit on`，避免进程退出时队列里还有未处理页面。
- 如果 VLM 成本较高，可降低 `--ui_collect_max_vlm_calls` 或关闭 `--ui_collect_use_vlm`。

## 常见问题

### `ModuleNotFoundError: No module named 'auto_explore'`

没有设置 `PYTHONPATH`。在项目根目录执行：

```powershell
$env:PYTHONPATH = "$PWD\auto_explore\src;$env:PYTHONPATH"
```

或使用仓库内脚本，它们会自动设置 `PYTHONPATH`。

### `Please provide OPENROUTER_API_KEY or --openrouter_api_key`

`auto_search` 要求 Explorer API Key。设置环境变量或显式传参：

```powershell
$env:OPENROUTER_API_KEY = "your-key"
```

如果使用 SJTU 服务，也可以在脚本里把 `OPENROUTER_API_KEY` 设置为 `SJTU_API_KEY`。

### Decider 服务无法连接

检查：

- `--decider_base_url` 是否是 OpenAI-compatible `/v1` 地址。
- `--decider_model` 是否与服务端模型名一致。
- 本地服务模式下 `--service_ip` 和 `--decider_port` 是否正确。
- 是否需要 `--decider_api_key`。

### App 启动后被判定不在前台

可能是 App 名称和包名映射不一致。检查 `runner.log` 中的 `Foreground app mismatch`，确认设备适配器能从 app name 找到正确 package。

### ADB endpoint 不通

检查：

```bash
adb connect <host:port>
adb devices
```

并确认并行配置里的 `adb_endpoint` 指向的是当前模拟器实例，而不是 MobileWorld backend 地址。

### 回溯或 replay recovery 失败率高

优先查看 `runner.log` 和 `metrics.json`：

- 动态 feed 页面可适当提高 `--page_load_wait_sec`。
- 页面结构变化慢时提高 `--page_load_stable_max_polls`。
- 如果 tab 误回溯，检查 hierarchy 中是否正确标注 `selected=true`。
- 如果 replay 动作失败，检查 `action_record` 是否包含完整坐标、文本或滑动坐标。

### UI 语义采集退出时仍有未完成任务

保持：

```text
--ui_collect_drain_on_exit on
--ui_collect_drain_timeout_sec 180
```

如果采集太慢，可以减少 `--ui_collect_max_items`、`--ui_collect_max_vlm_calls`，或关闭 VLM。

### Windows 中文 app 名或路径乱码

建议：

- 使用 `chcp 65001`，仓库脚本已设置。
- 尽量通过环境变量传中文 App 名：`set AUTO_EXPLORE_APP_NAME=应用名`。
- 避免把中文硬编码进 `.bat` 文件。
- 日志文件由脚本写入 UTF-8 BOM，推荐用 VS Code 打开。

## 快速自检

在项目根目录运行：

```powershell
$env:PYTHONPATH = "$PWD\auto_explore\src;$env:PYTHONPATH"
python -m auto_explore.cli.auto_search --help
python -m auto_explore.cli.parallel_runner --help
python -m auto_explore.cli.ablation_runner --help
python -m auto_explore.eval.cli --help
```

如果以上命令都能打印帮助信息，说明 `PYTHONPATH` 和 CLI 入口基本可用。
