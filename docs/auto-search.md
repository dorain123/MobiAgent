# Auto-Search 自动探索指南

## 1. 概述

`auto-search` 是 MobiAgent 的**自动页面探索模块**，用于对指定 App 进行无人值守的 DFS（深度优先搜索）遍历，自动生成可交互的操作路径。它不需要人工编写任务描述，而是通过两个模型协同工作：

| 模型角色 | 推荐模型 | 作用 |
|---------|---------|------|
| **Explorer** | `google/gemini-3-flash-preview` 或其他通用 VLM | 分析当前页面截图和 UI 层级结构，生成候选单步任务 |
| **Decider** | MobiAgent 系列（如 `MobiMind-Reasoning-4B`） | 将候选任务转换为精确的可执行动作（点击坐标、滑动方向、输入文本等） |

### 适用场景

- 新 App 的**自动化数据采集**
- 生成训练数据集（截图 + 动作 + 推理链）
- 探索 App 的功能覆盖范围
- 为后续的固定任务编写提供参考路径

---

## 2. 核心工作流程

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Auto-Search 主循环                            │
│                                                                     │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────────┐  │
│  │ Explorer │───▶│ Decider  │───▶│  执行动作  │───▶│  记录观测数据  │  │
│  │  生成候选  │    │  转为动作  │    │  (设备控制) │    │ (截图+层级)   │  │
│  └──────────┘    └──────────┘    └──────────┘    └──────────────┘  │
│       ▲                                              │              │
│       │                                              ▼              │
│       │                                         ┌──────────┐       │
│       └──────────────── 回溯/递归 ◀───────────── │ 深度检查  │       │
│                                                 └──────────┘       │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.1 详细步骤

```
步骤 1: 启动 App，截取当前页面，获取 UI 层级结构 (XML/JSON)
         ↓
步骤 2: Explorer 模型接收 [截图 + 层级文本 + 历史动作]，输出:
         {
           "candidates": [
             {"rank": 1, "single_step_task": "点击搜索框", "reason": "页面中央有搜索入口"},
             {"rank": 2, "single_step_task": "滑动查看更多", "reason": "列表未完全展示"},
             ...
           ],
           "popup": {"detected": true, "close_point": [950, 120]}  // 可选：弹窗检测
         }
         ↓
步骤 3: 对候选进行去重（与已探索任务比对语义相似度）
         ↓
步骤 4: 按 rank 顺序，逐个执行候选:
         4a. Decider 接收 [截图 + "帮我{single_step_task}"] → 输出精确动作
         4b. 执行动作 (click/swipe/input/wait)
         4c. 等待页面加载完成（固定等待 + 智能轮询）
         4d. 记录观测数据（截图、层级、高亮标注图）
         ↓
步骤 5: 如果未达到深度上限 → 递归进入步骤 1（新页面）
         如果达到深度上限 → 保存当前路径 (path_xxxx/)，追加 done 标记
         ↓
步骤 6: 回溯 → 执行返回操作 (back/home) → 验证是否回到上一页
         ↓
步骤 7: 尝试下一个候选，重复步骤 4-6
```

---

## 3. 关键组件

### 3.1 Explorer 模型

Explorer 使用通用 VLM，通过 OpenAI 兼容接口调用。核心 Prompt 结构：

```
系统提示: 你是一个移动 App 页面分析助手...

用户输入:
  - 当前页面截图 (image)
  - UI 层级结构文本 (XML/JSON → text)
  - 当前深度/广度参数
  - 已执行动作历史
  - 当前页面已探索过的任务列表

输出格式:
{
  "candidates": [...],
  "popup": {"detected": bool, "close_point": [x, y]}
}
```

**特性：**
- 支持弹窗/广告检测，返回关闭按钮坐标
- 可根据 `already_explored` 参数避免重复推荐相同任务
- 响应会被缓存（`ExplorerCache`），相同页面不重复调用

### 3.2 Decider 模型

Decider 使用 MobiAgent 专用模型，负责将自然语言任务转为精确动作：

```
输入: "当前处在美团，请帮我点击搜索框"
输出: {
  "action": "click",
  "parameters": {"bbox": [200, 400, 600, 480]},
  "reasoning": "观察到页面中央有搜索框..."
}
```

**动作类型：**
| 动作 | 参数 | 说明 |
|------|------|------|
| `click` | `bbox` 或 `point` | 点击指定区域 |
| `swipe` | `start`, `end`, `direction` | 滑动页面 |
| `input` | `text` | 输入文本 |
| `wait` | 无 | 等待页面加载 |

**优化：层级文本直接定位**
当 `--allow_hierarchy_text_decider on` 且目标文本在 UI 层级中存在时，直接解析 XML/JSON 获取 bbox，跳过 Decider 模型调用，节省时间和 API 费用。

### 3.3 DFS 引擎

深度优先搜索 + 回溯机制：

```python
explore_dfs(
    depth_limit=3,           # 最大深度
    breadth=4,               # 每层候选数
    current_depth=0,         # 当前深度
    path_actions=[],         # 当前路径的动作序列
    visited_tasks={},        # 已探索任务 {page_fp: {task1, task2, ...}}
)
```

**核心逻辑：**
1. 每层生成 `breadth` 个候选
2. 按 rank 顺序逐个执行
3. 执行到叶子节点（`current_depth >= depth_limit`）保存路径
4. 执行返回操作回溯，验证是否回到上一页
5. 尝试下一个候选

### 3.4 回溯验证机制

执行返回操作后，通过**三重验证**确保回到了正确的页面：

| 验证方式 | 原理 | 阈值 |
|---------|------|------|
| **文本指纹** | 归一化后的 UI 层级文本哈希 | 完全匹配 |
| **结构指纹** | UI 元素树的结构哈希 | 完全匹配 |
| **视觉 dHash** | 截图的感知哈希（差异哈希） | 汉明距离 ≤ 3 |

**2/3 通过**即认为回溯成功。

**失败恢复：** 如果回溯验证失败，尝试**全路径重播**——从 App 根节点重新执行路径上的所有前置动作。最多重试 2 次。

---

## 4. 参数说明

### 4.1 基础参数

| 参数 | 类型 | 必填 | 说明 | 示例 |
|------|------|------|------|------|
| `--app_name` | str | ✅ | 目标 App 名称（需与设备映射一致） | `"美团"` |
| `--depth` | int | ✅ | 探索深度，必须 > 0 | `3` |
| `--breadth` | int | ✅ | 每层候选数，必须 > 0 | `4` |
| `--device` | str | 否 | 设备类型 | `Android` / `Harmony` |
| `--service_ip` | str | 否 | Decider 服务 IP | `localhost` |
| `--decider_port` | int | 否 | Decider 服务端口 | `8000` |

### 4.2 Explorer 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--openrouter_base_url` | `https://openrouter.ai/api/v1` | Explorer 模型 API 地址 |
| `--openrouter_api_key` | 环境变量 `OPENROUTER_API_KEY` | API Key（**必填**） |
| `--explorer_model` | `google/gemini-3-flash-preview` | Explorer 模型名称 |

### 4.3 Decider 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--decider_base_url` | 空（使用 `service_ip:decider_port`） | 自定义 Decider Base URL |
| `--decider_api_key` | 空 | 自定义 Decider API Key |
| `--decider_model` | 空（使用占位符） | 自定义 Decider 模型名称 |
| `--use_qwen3` | `on` | 是否使用 Qwen3 坐标换算 |
| `--allow_hierarchy_text_decider` | `on` | 是否允许用 UI 层级文本直接定位（跳过模型） |

### 4.4 页面加载等待

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--page_load_wait_sec` | `1.5` | 动作后固定等待秒数（等页面开始渲染） |
| `--page_load_stable_max_polls` | `6` | 页面稳定轮询最大次数（每次 0.5s，总最大等待 = 次数 × 0.5s） |

**智能等待逻辑：**
```
动作执行 → 固定等待 1.5s → 轮询检测 hierarchy 是否稳定（最多 6 次，每次间隔 0.5s）
```

### 4.5 BBox 精炼

当 Decider 输出的 bbox 与 UI 层级中的元素匹配时，用 XML 元素的精确边框替换模型预测的 bbox：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--bbox_iou_threshold` | `0.3` | IoU >= 此值则采用 XML 元素边框 |
| `--bbox_center_dist_ratio` | `0.08` | 中心距/对角线 <= 此值才匹配 |
| `--bbox_area_ratio_min` | `0.5` | 候选元素面积 / 模型 bbox 面积下限 |
| `--bbox_area_ratio_max` | `2.0` | 候选元素面积 / 模型 bbox 面积上限 |

> **调参建议：** 换模型或换手机时，如果模型预测不准确，适当调低 `--bbox_iou_threshold`（如 `0.1`）

### 4.6 弹窗自动关闭

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--popup_dismiss_max_attempts` | `2` | 弹窗自动关闭最大尝试次数，`0` 表示禁用 |

**工作流程：**
```
Explorer 检测到弹窗 → 点击关闭按钮（或按返回键） → 等待页面稳定 → 重新调用 Explorer 确认弹窗已关闭
```

### 4.7 UI 语义采集（可选）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--enable_ui_semantic_collect` | `on` | 是否启用页面图标采集 |
| `--ui_collect_async` | `on` | 是否异步采集（`off` 等价于关闭采集） |
| `--ui_collect_use_vlm` | `on` | 采集是否启用 VLM |
| `--ui_collect_vlm_text_only` | `off` | 采集文本是否全走 VLM |
| `--ui_collect_vlm_model` | `qwen/qwen3-vl-30b-a3b-instruct` | 采集 VLM 模型 |
| `--ui_collect_max_items` | `32` | 页面采集最大元素数 |
| `--ui_collect_max_vlm_calls` | `12` | 页面采集 VLM 调用预算 |
| `--ui_collect_queue_size` | `8` | 页面采集任务队列容量 |
| `--ui_collect_drain_on_exit` | `on` | 退出前是否等待采集队列清空 |
| `--ui_collect_drain_timeout_sec` | `180` | 退出前等待采集队列清空的超时秒数 |

> **注意：** Explorer 和 UI Collect 可使用不同的模型提供商。Explorer 使用 `--openrouter_base_url` + `--explorer_model`，UI Collect 使用 `--ui_collect_base_url` + `--ui_collect_vlm_model`。

---

## 5. 输出目录结构

默认输出路径：`auto_explore/data/<app_name>/<timestamp>/`

```
auto_explore/data/美团/20260413-153022/
├── steps/                      # 单步原子结果
│   ├── step_0001/
│   │   ├── 1.jpg              # 执行该步前的截图
│   │   ├── 1.xml              # UI 层级 (Android) 或 1.json (Harmony)
│   │   ├── 1_highlighted.jpg  # 高亮标注图（红色文字+箭头/圆圈）
│   │   ├── 1_bounds.jpg       # bbox 标注图（红色矩形框）
│   │   ├── 1_click_point.jpg  # 点击位置图（绿色圆点）
│   │   ├── 1_swipe.jpg        # 滑动轨迹图（蓝色箭头）
│   │   ├── actions.json       # 单步动作记录
│   │   └── react.json         # 单步推理记录
│   ├── step_0002/
│   │   └── ...
│   └── ...
│
├── paths/                      # 完整 DFS 路径
│   ├── path_0001/
│   │   ├── 1.jpg              # 从 steps 复制并重编号的截图
│   │   ├── 1.xml
│   │   ├── 1_highlighted.jpg
│   │   ├── 2.jpg
│   │   ├── 2.xml
│   │   ├── ...
│   │   ├── n.jpg              # done 步骤对应的最终状态截图
│   │   ├── n.xml
│   │   ├── actions.json       # 整条路径的动作序列
│   │   └── react.json         # 整条路径的推理记录
│   └── ...
│
└── ui-pages/                   # 异步页面图标采集（可选）
    ├── page_0001/
    │   ├── snapshot/
    │   │   ├── input_screenshot.jpg     # 设备原始截图（未缩放）
    │   │   └── input_hierarchy.xml|json # 设备原始层级
    │   └── ...                  # VLM 处理后的图标/语义信息
    ├── pages_index.json         # 页面采集状态索引
    └── ...
```

### 5.1 `actions.json` 格式

```json
{
  "app_name": "美团",
  "task_type": "auto_search",
  "task_description": "打开美团，第一步：点击搜索框，第二步：输入'火锅'",
  "action_count": 2,
  "actions": [
    {
      "action_index": 1,
      "type": "click",
      "position_x": 540,
      "position_y": 280,
      "bounds": [400, 240, 680, 320],
      "source_task": "点击搜索框"
    },
    {
      "action_index": 2,
      "type": "input",
      "text": "火锅"
    }
  ]
}
```

### 5.2 `react.json` 格式

```json
[
  {
    "action_index": 1,
    "reasoning": "页面中央有明显的搜索框，点击后可输入关键词...",
    "function": {
      "name": "click",
      "parameters": {
        "target_element": "搜索框"
      }
    }
  },
  {
    "action_index": 2,
    "reasoning": "搜索框已激活，键盘已弹出...",
    "function": {
      "name": "input",
      "parameters": {
        "text": "火锅"
      }
    }
  }
]
```

### 5.3 `pages_index.json` 格式（UI 采集状态）

```json
{
  "page_0001": {
    "status": "ok",
    "attempt_count": 1,
    "created_at": "2026-04-13T15:30:22",
    "snapshot": {
      "screenshot_path": "page_0001/snapshot/input_screenshot.jpg",
      "hierarchy_path": "page_0001/snapshot/input_hierarchy.xml"
    },
    "dedupe_key": {
      "raw_fp": "abc123...",
      "struct_fp": "def456...",
      "dhash": "7f3a..."
    }
  }
}
```

`status` 可能值：`queued` | `running` | `ok` | `failed` | `skipped_queue_full` | `skipped_shutdown_timeout` | `skipped_dedup`

---

## 6. 优化机制

### 6.1 Explorer 响应缓存 (`ExplorerCache`)

- **原理：** 对相同的页面（结构指纹）+ 深度 + 已探索任务集合，缓存 Explorer 返回的候选列表
- **TTL：** 默认 300 秒
- **效果：** 回溯到相同页面时不重复调用 Explorer API，节省时间和费用

### 6.2 设备状态缓存 (`ScreenStateCache`)

- **原理：** 合并 `screenshot` + `hierarchy` 采集，设置过期时间（默认 0.3s）
- **效果：** 避免在短时间内重复调用设备接口

### 6.3 候选语义去重

```python
# 过滤与已探索任务相似度 > sim_threshold 的候选
from difflib import SequenceMatcher
ratio = SequenceMatcher(None, candidate_task, explored_task).ratio()
if ratio > 0.8:  # sim_threshold 默认 0.8
    skip(candidate)
```

### 6.4 自适应页面变化检测

当执行动作后页面发生变化（如弹窗消失、广告关闭），系统会：
1. 重新调用 Explorer 生成新的候选列表
2. 动态调整相似度阈值（基于页面复杂度）

### 6.5 路径重播恢复

回溯验证失败时的恢复流程：
```
1. 重新启动 App
2. 从路径的第一个动作开始重播
3. 每执行一个动作后验证是否匹配预期状态
4. 重播成功后继续后续探索
5. 最多重试 2 次
```

---

## 7. 运行方式

### 7.1 使用模板脚本（推荐）

**Linux/macOS:**
```bash
# 编辑模板脚本中的参数
vim auto_explore/scripts/run_single.sh

# 设置环境变量
export OPENROUTER_API_KEY="your-key-here"

# 执行
bash auto_explore/scripts/run_single.sh
```

**Windows:**
```cmd
auto_explore/scripts/run_single.bat
```

### 7.2 直接命令行

```bash
PYTHONPATH=auto_explore/src python -m auto_explore.cli.auto_search \
  --app_name "美团" \
  --depth 3 \
  --breadth 4 \
  --device Android \
  --service_ip localhost \
  --decider_port 8000 \
  --openrouter_api_key "$OPENROUTER_API_KEY" \
  --explorer_model "google/gemini-3-flash-preview" \
  --use_qwen3 on \
  --allow_hierarchy_text_decider on \
  --enable_ui_semantic_collect on \
  --ui_collect_async on \
  --popup_dismiss_max_attempts 2
```

---

## 8. 推荐实践

### 8.1 参数调优建议

| 阶段 | depth | breadth | 说明 |
|------|-------|---------|------|
| 初次验证 | 2 | 2 | 快速验证连通性和基本功能 |
| 小规模探索 | 3 | 4 | 覆盖大部分常用路径 |
| 全面探索 | 5+ | 6-10 | 深度覆盖 App 的各种功能 |

### 8.2 故障排查

| 问题 | 可能原因 | 解决方案 |
|------|---------|---------|
| Explorer 无输出 | API Key 无效 / 模型不可用 | 检查 `OPENROUTER_API_KEY` 和 `--explorer_model` |
| Decider 无响应 | 服务未启动 / 端口错误 | 确认 vLLM 服务运行正常，端口匹配 |
| App 未启动 | App 名称不匹配 | 检查 `--app_name` 是否与设备映射表一致 |
| 回溯失败 | 页面动态内容 | 调大 `--page_load_wait_sec` 或 `--page_load_stable_max_polls` |
| 路径异常 | 弹窗/广告干扰 | 开启 `--popup_dismiss_max_attempts 2` |
| UI 采集慢 | VLM 调用耗时 | 调小 `--ui_collect_max_vlm_calls` 或关闭采集 |

### 8.3 性能与成本

- **Explorer 调用：** 每次新页面调用一次，缓存命中则跳过
- **Decider 调用：** 每个候选动作调用一次（除非 `--allow_hierarchy_text_decider` 命中）
- **UI 采集：** 每个新页面最多 `--ui_collect_max_vlm_calls` 次
- **存储：** 每步约 1-3MB（截图 + XML + 标注图），完整路径更多

### 8.4 注意事项

1. **App 映射：** `--app_name` 必须在设备的 App 映射表中存在
2. **设备连通性：** 确认 ADB 连接正常（`adb devices` 可见）
3. **API 费用：** Explorer 和 Decider 都调用远程模型，注意控制预算
4. **存储清理：** 大规模探索会生成大量截图，定期清理 `auto_explore/data/` 目录
5. **后台限制：** 某些 Android 厂商会限制后台应用的 CPU/内存，请在开发者选项中关闭电池优化

---

## 9. 架构总览

```
┌─────────────────────────────────────────────────────────────────────┐
│                          Auto-Search 架构                            │
│                                                                     │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐          │
│  │   Explorer   │    │   Decider    │    │   UI Collect │          │
│  │   (VLM)      │    │   (MobiMind) │    │   (VLM)      │          │
│  │              │    │              │    │   (optional) │          │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘          │
│         │                   │                   │                   │
│         ▼                   ▼                   ▼                   │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │                    DFS 引擎 (explore_dfs)                     │  │
│  │                                                              │  │
│  │  • 候选生成 (call_explorer_model)                            │  │
│  │  • 候选去重 (_deduplicate_candidates)                         │  │
│  │  • 单步执行 (execute_decider_one_step)                       │  │
│  │  • 回溯验证 (三重验证 + 路径重播)                             │  │
│  │  • 路径保存 (copy_step_artifacts_to_path)                    │  │
│  │  • Explorer 缓存 (ExplorerCache)                             │  │
│  │  • 设备状态缓存 (ScreenStateCache)                            │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                              │                                     │
│                              ▼                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │                      设备控制层                               │  │
│  │                                                              │  │
│  │  • Android: uiautomator2                                     │  │
│  │  • Harmony:   hmdriver2                                      │  │
│  │                                                              │  │
│  │  操作: screenshot, dump_hierarchy, click, swipe, input, back │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                              │                                     │
│                              ▼                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │                      数据持久层                               │  │
│  │                                                              │  │
│  │  auto_explore/data/<app>/<timestamp>/                        │  │
│  │    ├── steps/   (单步原子结果)                                │  │
│  │    ├── paths/   (完整 DFS 路径)                               │  │
│  │    └── ui-pages/ (页面图标采集)                               │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 10. 相关文件

| 文件 | 说明 |
|------|------|
| `auto_explore/src/auto_explore/cli/auto_search.py` | Auto-Search 核心实现 |
| `auto_explore/README.md` | Auto Explore 使用说明 |
| `auto_explore/scripts/run_single.sh` | Linux/macOS 运行参数模板 |
| `auto_explore/scripts/run_single.bat` | Windows 运行参数模板 |
| `runner/mobiagent/mobiagent.py` | Decider 模型调用 + 设备控制基础 |
| `collect/auto/ui_semantic_boxer` | UI 语义采集模块 |
| `prompts/e2e_qwen3_system.md` | Decider 系统提示模板 |
