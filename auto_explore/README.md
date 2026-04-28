# Auto Explore

`auto_explore/` 是仓库内独立维护的自动探索子项目，负责：

- 单机自动探索
- 多模拟器并发调度
- 模拟器 JSON 配置管理
- 自动探索脚本、测试和输出目录管理

当前主入口：

- `python -m auto_explore.cli.auto_search`
- `python -m auto_explore.cli.parallel_runner`
- `python -m auto_explore.eval.cli`

## 目录说明

```text
auto_explore/
├── README.md
├── configs/
│   └── simulators.example.json
├── scripts/
│   ├── run_single.sh
│   ├── run_parallel.sh
│   └── run_single.bat
├── src/
│   └── auto_explore/
│       ├── cli/
│       │   ├── auto_search.py
│       │   └── parallel_runner.py
│       └── adapters/
└── data/
```

## 1. 环境准备

### 1.1 工作目录

所有命令都建议在仓库根目录执行：

```bash
cd ./MobiAgent
```

如果你当前就在 `auto_explore/` 目录下，也可以直接执行脚本。`run_single.sh` 和 `run_parallel.sh` 会根据脚本自身位置自动定位仓库根目录，不依赖当前 `PWD`。

### 1.2 Python 环境

激活python环境,和运行mobiagent环境一致。

### 1.3 PYTHONPATH

`auto_explore` 代码位于 `auto_explore/src` 下，直接命令行运行时需要设置：

```bash
export PYTHONPATH="${PWD}/auto_explore/src${PYTHONPATH:+:${PYTHONPATH}}"
```

如果使用 `auto_explore/scripts/run_single.sh` 或 `auto_explore/scripts/run_parallel.sh`，脚本内部已经自动设置了 `PYTHONPATH`。

### 1.4 必要服务和依赖

启动收集前，至少需要确认以下条件成立：

- 设备或模拟器在线
- Decider 服务可访问
- Explorer 模型服务可访问
- `OPENROUTER_API_KEY` 已设置
- 如果使用多模拟器并发，`MobileWorld` backend 可访问，且每台模拟器对应的 backend/adb 端口已配置正确

最常用环境变量：

```bash
export OPENROUTER_API_KEY="your-openrouter-key"
export DECIDER_API_KEY=""
```

## 2. 配置说明

### 2.1 单机收集配置

单机模板脚本是：

- [run_single.sh](/home/zhaoxi/ipads/llm-agent/test/MobiAgent/auto_explore/scripts/run_single.sh)
- [run_single.bat](/home/zhaoxi/ipads/llm-agent/test/MobiAgent/auto_explore/scripts/run_single.bat)

关键参数：

- `APP_NAME`
  目标 App 名称，例如 `微博`、`美团`
- `DEPTH`
  探索深度。建议先从 `2` 开始验证
- `BREADTH`
  每层候选广度。建议先从 `2` 开始验证
- `DEVICE`
  `Android` 或 `Harmony`
- `DECIDER_BASE_URL`
  Decider 模型服务地址
- `DECIDER_MODEL`
  Decider 模型名
- `OPENROUTER_BASE_URL`
  Explorer 模型服务地址
- `EXPLORER_MODEL`
  Explorer 模型名
- `DATA_DIR`
  可选。为空时自动输出到 `auto_explore/data/<app>/<timestamp>/`

几个常用开关：

- `ALLOW_HIERARCHY_TEXT_DECIDER`
  是否允许基于 hierarchy 文本直接定位目标
- `ENABLE_UI_SEMANTIC_COLLECT`
  是否启用 UI 语义采集
- `POPUP_DISMISS_MAX_ATTEMPTS`
  自动关闭弹窗的最大尝试次数

建议的初始值：

```bash
APP_NAME="微博"
DEPTH=2
BREADTH=2
DEVICE="Android"
ENABLE_UI_SEMANTIC_COLLECT="on"
POPUP_DISMISS_MAX_ATTEMPTS=2
```

### 2.2 多模拟器并发配置

多机并发模板脚本是 [run_parallel.sh](/home/zhaoxi/ipads/llm-agent/test/MobiAgent/auto_explore/scripts/run_parallel.sh)。

并发模式比单机多两类配置：

- 模拟器列表
- 启动前快照初始化任务列表

#### 模拟器 JSON

默认配置文件是 [simulators.example.json](/home/zhaoxi/ipads/llm-agent/test/MobiAgent/auto_explore/configs/simulators.example.json)：

```json
{
  "simulators": [
    {
      "name": "模拟器1",
      "backend_url": "http://123.60.91.241:9000",
      "adb_endpoint": "123.60.91.241:8000",
      "init_device": "emulator-5554"
    },
    {
      "name": "模拟器2",
      "backend_url": "http://123.60.91.241:9001",
      "adb_endpoint": "123.60.91.241:8080",
      "init_device": "emulator-5554"
    }
  ]
}
```

字段含义：

- `name`
  逻辑名称，用于日志和输出目录
- `backend_url`
  `MobileWorld` backend 地址，用于健康检查和任务初始化
- `adb_endpoint`
  设备连接地址，传给自动探索进程（用于采集）
- `init_device`
  快照初始化时传给 backend `/init` 和 `/task/init` 的设备 ID，默认是 `emulator-5554`

#### 快照初始化任务

并发脚本中的 `TASK_NAMES` 用于在每台模拟器启动前顺序恢复任务初始状态：
任务名建议先从 [task_categories.md](/home/zhaoxi/ipads/llm-agent/test/MobiAgent/MobileWorld/docs/task_categories.md) 里选择。

```bash
TASK_NAMES=(
  "MattermostCreateChannel"
  "GmailSendEmail"
)
```

执行顺序是：

1. 检查 backend 健康状态
2. 初始化设备
3. 拉取 backend 可用任务列表并校验 `TASK_NAMES` 是否存在
4. 依次对每台模拟器执行 `task_name` 初始化
5. 每台模拟器停留在最后一个 `task_name` 对应的状态
6. 再启动 auto-search 收集

这里目前只支持 `task_name`，不支持直接传 `snapshot_tag`。
同时，`task_name` 只用于恢复环境，真正收集目标仍由 `APP_NAME` 决定。

#### 额外模拟器

如果不想改 JSON，也可以在脚本中追加：

```bash
EXTRA_SIMULATORS=(
  "模拟器3|http://127.0.0.1:9002|127.0.0.1:8010"
)
```

## 3. 启动流程

### 3.1 单机收集流程

1. 切到仓库根目录
2. 激活 `collect` 环境
3. 设置 `OPENROUTER_API_KEY`
4. 编辑 [run_single.sh](/home/zhaoxi/ipads/llm-agent/test/MobiAgent/auto_explore/scripts/run_single.sh)
5. 执行脚本

示例：

```bash
cd /home/zhaoxi/ipads/llm-agent/test/MobiAgent
conda activate collect
export OPENROUTER_API_KEY="your-openrouter-key"
bash auto_explore/scripts/run_single.sh
```

### 3.2 多模拟器并发流程

1. 切到仓库根目录
2. 激活 `collect` 环境
3. 设置 `OPENROUTER_API_KEY`
4. 修改 [simulators.example.json](/home/zhaoxi/ipads/llm-agent/test/MobiAgent/auto_explore/configs/simulators.example.json)
5. 修改 [run_parallel.sh](/home/zhaoxi/ipads/llm-agent/test/MobiAgent/auto_explore/scripts/run_parallel.sh) 中的 `TASK_NAMES`、模型参数和 App 参数
6. 执行脚本

示例：

```bash
cd /home/zhaoxi/ipads/llm-agent/test/MobiAgent
conda activate collect
export OPENROUTER_API_KEY="your-openrouter-key"
bash auto_explore/scripts/run_parallel.sh
```

## 4. 直接命令行执行

### 4.1 单机直接执行

如果不走脚本，可以直接运行：

```bash
cd /home/zhaoxi/ipads/llm-agent/test/MobiAgent
conda activate collect
export PYTHONPATH="${PWD}/auto_explore/src${PYTHONPATH:+:${PYTHONPATH}}"
export OPENROUTER_API_KEY="your-openrouter-key"

python -m auto_explore.cli.auto_search \
  --app_name "微博" \
  --depth 2 \
  --breadth 2 \
  --device Android \
  --decider_base_url "http://166.111.53.96:7003/v1" \
  --decider_api_key "${DECIDER_API_KEY:-}" \
  --decider_model "MobiMind-1.5-4B" \
  --openrouter_base_url "https://openrouter.ai/api/v1" \
  --openrouter_api_key "$OPENROUTER_API_KEY" \
  --explorer_model "qwen/qwen3-vl-235b-a22b-instruct" \
  --use_qwen3 on \
  --allow_hierarchy_text_decider off \
  --enable_ui_semantic_collect on \
  --ui_collect_async on \
  --popup_dismiss_max_attempts 2
```

### 4.2 并发直接执行

```bash
cd /home/zhaoxi/ipads/llm-agent/test/MobiAgent
conda activate collect
export PYTHONPATH="${PWD}/auto_explore/src${PYTHONPATH:+:${PYTHONPATH}}"
export OPENROUTER_API_KEY="your-openrouter-key"

python -m auto_explore.cli.parallel_runner \
  --simulator-file auto_explore/configs/simulators.example.json \
  --task-name MattermostCreateChannel GmailSendEmail \
  --app_name 微博 \
  --depth 2 \
  --breadth 2 \
  -- \
  --device Android \
  --decider_base_url http://166.111.53.96:7003/v1 \
  --decider_api_key "${DECIDER_API_KEY:-}" \
  --decider_model MobiMind-1.5-4B \
  --openrouter_base_url https://openrouter.ai/api/v1 \
  --openrouter_api_key "$OPENROUTER_API_KEY" \
  --explorer_model qwen/qwen3-vl-235b-a22b-instruct
```

这里 `--` 后面的参数会原样透传给每个 auto-search 子进程。

## 5. 实际执行收集时会发生什么

### 5.1 单机模式

单机模式流程：

1. 启动目标 App
2. 获取截图和 hierarchy
3. Explorer 生成候选单步任务
4. Decider 将候选转成动作
5. 执行动作并等待页面稳定
6. 保存截图、标注图、层级和动作记录
7. 递归继续 DFS 探索
8. 到达叶子节点后保存完整路径

### 5.2 并发模式

并发模式比单机多前置初始化：

1. 读取模拟器 JSON
2. 对每台模拟器做 backend 健康检查
3. 对每台模拟器初始化设备并校验 `TASK_NAMES` 在 backend 中存在
4. 对每台模拟器执行 `TASK_NAMES` 指定的初始化任务
5. 为每台模拟器启动独立 `auto_search` 子进程
6. 每个子进程使用自己的 `adb_endpoint`
7. 每个子进程写入自己的输出目录和日志

## 6. 输出目录和日志

默认输出根目录：

```text
auto_explore/data/<app_name>/<timestamp>/
```

单机模式通常会直接在这个目录下生成：

- `steps/`
- `paths/`
- `ui-pages/`

并发模式会先按模拟器分目录：

```text
auto_explore/data/<app_name>/<timestamp>/
├── 模拟器1/
│   ├── runner.log
│   ├── steps/
│   ├── paths/
│   └── ui-pages/
└── 模拟器2/
    ├── runner.log
    ├── steps/
    ├── paths/
    └── ui-pages/
```

关键文件：

- `runner.log`
  每个模拟器对应的运行日志
- `steps/`
  单步动作原子结果
- `paths/`
  完整 DFS 路径结果
- `ui-pages/`
  UI 语义采集结果

## 7. 常见操作建议

### 7.1 第一次验证

第一次跑时建议：

- `DEPTH=2`
- `BREADTH=2`
- `ENABLE_UI_SEMANTIC_COLLECT=on`
- 先单机跑通，再开并发

### 7.2 并发前检查

启动并发前建议手工确认：

- `backend_url/health` 可访问
- `adb_endpoint` 对应的设备确实在线
- `TASK_NAMES` 中的任务在 backend 中存在
- `APP_NAME` 在设备映射表里存在

### 7.3 输出目录过大

自动探索会生成大量截图和标注文件，定期清理：

```bash
rm -rf auto_explore/data/*
```

只在你确认旧数据不再需要时再清理。

## 8. 常见问题

### 8.1 `OPENROUTER_API_KEY` 未设置

脚本会直接报错。先执行：

```bash
export OPENROUTER_API_KEY="your-openrouter-key"
```

### 8.2 并发启动前初始化失败

通常是这几类问题：

- `backend_url` 不通
- backend 不健康
- `task_name` 不存在
- 设备初始化失败

这时优先用 [snapshot_manager.py](/home/zhaoxi/ipads/llm-agent/test/MobiAgent/auto_explore/scripts/snapshot_manager.py) 单独验证对应 backend。

### 8.3 App 启动失败

说明 `APP_NAME` 与设备映射表不匹配，或者目标 App 当前不在设备环境中。

### 8.4 没有生成路径结果

优先检查：

- `DEPTH` 是否太小
- `BREADTH` 是否太小
- 模型服务是否可用
- 设备交互是否正常
- `runner.log` 中是否存在连续报错

## 9. 推荐执行顺序

推荐按这个顺序使用：

1. 修改 [run_single.sh](/home/zhaoxi/ipads/llm-agent/test/MobiAgent/auto_explore/scripts/run_single.sh)，先完成单机验证
2. 确认输出目录和日志正常
3. 修改 [simulators.example.json](/home/zhaoxi/ipads/llm-agent/test/MobiAgent/auto_explore/configs/simulators.example.json)
4. 修改 [run_parallel.sh](/home/zhaoxi/ipads/llm-agent/test/MobiAgent/auto_explore/scripts/run_parallel.sh) 中的 `TASK_NAMES`
5. 启动并发收集

如果只是想看参数面，可直接执行：

```bash
PYTHONPATH=auto_explore/src python -m auto_explore.cli.auto_search --help
PYTHONPATH=auto_explore/src python -m auto_explore.cli.parallel_runner --help
```

## 10. 评测现有轨迹

如果已经有 `auto_explore/results/...` 下的轨迹结果，可以直接运行评测：

```bash
PYTHONPATH=auto_explore/src python -m auto_explore.eval.cli \
  --input_path auto_explore/results/single/20260424_180926_淘宝 \
  --target_level auto \
  --judge_model qwen3vl
```

Windows 下也可以直接运行：

```bat
set SJTU_API_KEY=your-sjtu-key
auto_explore\scripts\run_eval.bat
```

其中 `run_eval.bat` 默认使用 `target_level=auto`，会优先评估 `paths/`，如果当前结果目录没有 `path_*` 样本，就自动回落到 `steps/`。

## Multimodal Path Eval

`auto_explore.eval.cli` also supports a dedicated path-level multimodal judge mode:

```bash
PYTHONPATH=auto_explore/src python -m auto_explore.eval.cli \
  --input_path auto_explore/results/ablation/20260423_151917_淘宝/E0_full/run_001 \
  --target_level paths \
  --judge_mode path_multimodal \
  --judge_model qwen3vl
```

This mode is path-only and expects numbered screenshots inside each `path_*` directory. It does not support `step_*` evaluation.
