# MobiAgent — QWEN.md

## 项目概述

**MobiAgent** 是一个强大且可定制的移动端 Agent 系统，由上海交通大学 IPADS-SAI 实验室开发。它包含三大核心组件：

| 组件 | 说明 |
|------|------|
| **MobiMind** | Agent 模型家族（Decider / Grounder / Planner），基于 Qwen-VL 系列微调 |
| **AgentRR** | Agent Record & Replay 框架，通过缓存和重用成功操作序列加速任务执行 |
| **MobiFlow** | 基于 DAG 的 Agent 评估基准，支持多路径离线验证 |

系统通过 ADB 连接 Android/HarmonyOS 设备，由 LLM 驱动自动完成跨 App 的复杂操作（搜索、购物、社交、导航等）。

## 技术栈

- **语言**: Python 3.10+
- **深度学习**: PyTorch, vLLM, Transformers, PaddlePaddle/PaddleOCR
- **LLM 框架**: OpenAI API 兼容接口, LangChain, LlamaIndex
- **移动端控制**: uiautomator2, hmdriver2 (鸿蒙), ADB
- **视觉**: OpenCV, Ultralytics (YOLO), OmniParser, supervision
- **记忆系统**: Mem0AI (用户偏好), Milvus (向量检索), Neo4j (GraphRAG)
- **Web**: FastAPI, Uvicorn

## 项目结构

```
MobiAgent/
├── agent_rr/            # Agent Record & Replay 训练与评估
├── app/                 # MobiAgent Android 应用
├── assets/              # 图片资源 (架构图、Logo 等)
├── collect/             # 数据采集、标注、构建工具
│   ├── manual/          # 手动采集 Web UI
│   ├── auto/            # 自动采集
│   └── annotate/        # LLM 视觉标注与 SFT 数据集构建
├── deployment/          # 服务部署 (server.py)
├── MobiFlow/            # 基于 DAG 的离线验证框架
│   └── avdag/           # 核心验证引擎 (条件检查器、DAG 分析)
├── phone_runner/        # 手机端纯本地推理 (Termux + MNN AWQ 量化模型)
├── prompts/             # 各模块 Prompt 模板 (Decider/Grounder/Planner/E2E)
├── runner/              # Agent 执行器
│   ├── mobiagent/       # 主 Agent Runner (含 multi_task)
│   └── UI-TARS-agent/   # UI-TARS 模型 Runner
└── utils/               # 工具库 (经验记忆、OCR、Prompt 加载等)
```

## 环境搭建

### 基础环境

```bash
conda create -n MobiMind python=3.10
conda activate MobiMind
```

### 依赖安装

```bash
# 最小依赖 (仅运行 Agent Runner)
pip install -r requirements_simple.txt

# 完整依赖 (含训练、数据采集等全功能)
pip install -r requirements.txt
```

### 移动端准备

1. Android 设备安装 [ADBKeyboard](https://github.com/senzhk/ADBKeyBoard/blob/master/ADBKeyboard.apk)
2. 开启开发者选项 → USB 调试
3. USB 连接手机，确认 `adb devices` 可见

### 模型部署 (vLLM)

```bash
# Decider + Grounder (MobiMind-Reasoning-4B 共用端口)
vllm serve MobiMind-Reasoning-4B --port <decider_port>

# Planner
vllm serve Qwen/Qwen3-4B-Instruct --port <planner_port>
```

## 运行与使用

### 启动 Agent Runner

```bash
python -m runner.mobiagent.mobiagent \
  --service_ip localhost \
  --decider_port 8000 \
  --planner_port 8002
```

常用参数：

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--service_ip` | 服务 IP | `localhost` |
| `--decider_port` | Decider 端口 | `8000` |
| `--planner_port` | Planner 端口 | `8002` |
| `--e2e` | 使用端到端模型，跳过 Grounder | `false` |
| `--device` | 设备类型 `Android` / `Harmony` | `Android` |
| `--user_profile` | 启用用户画像记忆 `on`/`off` | `off` |
| `--use_experience` | 启用经验重写 | `off` |
| `--data_dir` | 结果保存目录 | `runner/mobiagent/data/` |
| `--task_file` | 任务列表文件路径 | `runner/mobiagent/task.json` |

### 多任务执行

```bash
python -m runner.mobiagent.multi_task.mobiagent_refactored \
  --service_ip localhost \
  --decider_port 8000 \
  --grounder_port 8000 \
  --planner_port 8002 \
  --task "在小红书查找相机推荐，然后淘宝搜索并微信发送给朋友"
```

### 数据采集

```bash
# 手动采集 — 启动 Web UI (localhost:9000)
python -m collect.manual.server

# 自动采集
python -m collect.auto.server --model <model_name> --api_key <key> --base_url <url>

# 视觉标注
python -m collect.annotate --data_path <data_path> --model <model> --api_key <key> --base_url <url>

# 构建 SFT 数据集
python -m collect.construct_sft --data_path <raw_data> --out_path <output>
```

### MobiFlow 离线验证

```bash
python -m avdag.verifier task_configs/taobao.json trace_folder/
```

### AgentRR 实验

```bash
# 准备训练数据
python -m train.prepare_data --task both --train_path <train> --test_path <test>

# 运行实验
python run_experiment.py --data_path <test_data> --embedder_path <embedding_model> \
  --reranker_path <reranker_model> --ditribution uniform
```

### 手机端本地运行 (phone_runner)

在 Android Termux 环境中运行量化 MNN 模型，详见 `phone_runner/README.md`。

## 记忆系统

MobiAgent 支持三种记忆增强：

| 类型 | 说明 | 启用方式 |
|------|------|----------|
| **用户画像记忆** | Mem0 系统提取和检索用户偏好 | `--user_profile on` |
| **经验记忆** | 检索相似历史任务经验 | `--use_experience` |
| **动作记忆 (AgentRR)** | 缓存成功操作序列并回放 | 见 `agent_rr/` |

用户画像记忆支持两种后端：
- **Milvus** (向量检索): `--use_graphrag off`
- **Neo4j** (GraphRAG): `--use_graphrag on`

## 开发约定
- 当前可用开发环境: conda activate collect
- **Python 版本**: 3.10+
- **编码风格**: 代码中有 `abc.ABC` 抽象基类，遵循 OOP 设计
- **日志**: 使用标准 `logging` 模块，级别 `INFO`，输出到 stdout
- **配置**: 通过 `.env` 文件管理敏感配置 (API Key, DB URL 等)，使用 `dotenv` 加载
- **Prompt 管理**: Prompt 模板统一放在 `prompts/` 目录，通过 `utils/load_md_prompt.py` 加载
- **数据格式**:
  - `actions.json` — 操作序列及元数据
  - `react.json` — LLM 推理与决策记录
  - 截图命名 `1.jpg`, `2.jpg` ... 与步骤索引对应
- **忽略文件**: `data/`, `log/`, `weights/`, `__pycache__/`, `*.jpg` 等均在 `.gitignore` 中，不要提交

## 关键文件速查

| 文件 | 说明 |
|------|------|
| `runner/mobiagent/mobiagent.py` | 主 Agent Runner 入口，含完整决策循环 |
| `runner/mobiagent/task.json` | 任务列表配置文件 |
| `MobiFlow/avdag/verifier.py` | DAG 验证核心引擎 |
| `collect/` | 数据采集与数据集构建工具 |
| `prompts/` | 所有 Prompt 模板 |
| `utils/local_experience.py` | 经验记忆检索 (PromptTemplateSearch) |
| `utils/load_md_prompt.py` | Prompt 加载工具 |
| `phone_runner/` | 手机端本地推理方案 |
| `deployment/server.py` | 服务部署脚本 |
