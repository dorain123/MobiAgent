# CHANGELOG

## 2026-03-30

### OPT-2：增强回溯验证 + 全路径重播恢复

**文件**：`MobiAgent/runner/mobiagent/auto-search.py`

#### 问题
原有回溯逻辑存在两个缺陷：
1. 只用单一文本指纹（SHA1）判断是否回到正确页面，结构相同但内容不同的页面会碰撞误判
2. 验证失败后只重播 `current_path_actions[-2]` 一步，不验证前置状态也不验证执行结果

#### 修改内容

**修改 1**（第 1753-1760 行）：候选执行前新增基准状态采集

- 新增 `get_screenshot()` 调用，更新临时截图文件
- 新增 `pre_struct_fp`：结构指纹（仅 clickable 元素）
- 新增 `pre_dhash`：视觉 dHash（来自 `_compute_dhash_hex`）
- 用于回溯后的三重比对基准

**修改 2**（第 1888-1952 行）：替换回溯验证+恢复逻辑

旧逻辑：
- 单一文本指纹比对
- 失败则重播 `[-2]` 一步（无前置验证，无结果验证）

新逻辑：
- **三重验证**：文本指纹 + 结构指纹 + 视觉 dHash，2/3 通过视为回溯成功
- **全路径重播恢复**：验证失败时，`start_app()` 回根页面，循环重播 `current_path_actions[:-1]`，最多重试 2 次
- 每次重播后再次三重验证
- 全部失败则 `break` 跳出候选循环，防止后续兄弟候选在错误页面执行污染数据

#### 复用的已有函数（无新增函数）

| 函数 | 原位置 |
|------|--------|
| `_hierarchy_fingerprint()` | 第 575 行 |
| `_compute_hierarchy_struct_fingerprint()` | 第 705 行 |
| `_compute_dhash_hex()` | 第 646 行 |
| `get_screenshot()` | mobiagent.py 第 485 行 |
| `replay_action_record()` | 第 499 行 |

---

### OPT-5：候选动作执行前语义去重

**文件**：`MobiAgent/runner/mobiagent/auto-search.py`

#### 问题
Explorer 模型经常返回语义近似的候选（如"点击设置"与"进入设置界面"），浪费 Decider 调用和 DFS 分支预算。

#### 修改内容

**新增函数** `_deduplicate_candidates(candidates, already_explored, sim_threshold=0.75)`：
- 过滤与 `already_explored`（当前页已探索任务）相似度 >0.8 的候选
- 过滤候选列表内部相似度 >0.75 的重复项，保留 rank 更高者
- 使用 `difflib.SequenceMatcher`，无需外部依赖

**调用位置**：
- Explorer 返回后立即调用
- 动态重生成候选后也调用

---

### OPT-6：已探索路径注入 Explorer Prompt + 路径历史替代全局历史

**文件**：`MobiAgent/runner/mobiagent/auto-search.py`

#### 问题
1. Explorer 收到全局 `actions`（混入所有已回溯分支），无法判断当前设备处于哪条路径，生成候选语义脱节
2. Explorer 不知道当前页面哪些操作已被探索，重生成时重复已执行的动作

#### 修改内容

**`build_explorer_prompt`** 新增 `already_explored: Optional[List[str]]` 参数：
- 若非空，在 prompt 末尾追加"已在当前页面完成探索的操作（请勿重复生成）"段落

**`call_explorer_model`** 新增 `already_explored` 参数并透传给 `build_explorer_prompt`

**`explore_dfs`** 新增 `visited_tasks: Optional[Dict[str, set]]` 参数：
- 每次候选执行后：`visited_tasks[current_page_fp].add(task)`
- DFS 入口：`action_history` 改为 `path_actions`（当前路径）而非全局 `actions`
- Explorer 调用时传入 `already_explored_list = list(visited_tasks.get(fp, set()))`
- 递归调用时透传 `visited_tasks`

**`main()`** 初始化 `visited_tasks: Dict[str, set] = {}`

---

### OPT-9：Explorer 响应缓存

**文件**：`MobiAgent/runner/mobiagent/auto-search.py`

#### 问题
回溯返回同一页面后，下一个兄弟候选重新调用 Explorer API，但页面完全相同，浪费 API 费用和延迟。

#### 修改内容

**新增类** `ExplorerCache`：
- Cache key：`struct_fp | depth | breadth | hash(already_explored)`
- TTL：300 秒（防止动态内容污染）
- `get()` 返回缓存候选并过滤已执行任务；`put()` 存入新结果

**`explore_dfs`** 新增 `explorer_cache: Optional[ExplorerCache]` 参数：
- Explorer 调用前先查缓存，命中则跳过 API 调用
- 未命中时调用后写入缓存

**`main()`** 初始化 `explorer_cache = ExplorerCache(ttl_sec=300.0)`

---

### OPT-10：设备状态缓存

**文件**：`MobiAgent/runner/mobiagent/auto-search.py`

#### 问题
`get_screenshot()` 和 `get_hierarchy_text()` 在 DFS 多处被重复调用，`dump_hierarchy()` 实际耗时 0.5-2 秒。

#### 修改内容

**新增类** `ScreenStateCache`：
- `capture(device, device_type, force)` 在 staleness 阈值（0.3 秒）内返回缓存值，否则重新采集
- `invalidate()` 动作执行后立即失效

**`explore_dfs`** 新增 `screen_cache: Optional[ScreenStateCache]` 参数：
- DFS 入口的截图+层级采集改为 `screen_cache.capture(force=True)`
- 动作执行后调用 `screen_cache.invalidate()`
- 递归调用时透传

**`main()`** 初始化 `screen_cache = ScreenStateCache(staleness_sec=0.3)`



### 4.6
#### 问题1
经常小箭头返回导致死循环
#### 修改内容
修改prompt，告诉模型尽量不点击小箭头

#### 问题2
更改手机和模型后框识别有无
#### 修改内容
bat中新增4个可调参数
`BBOX_IOU_THRESHOLD`	IoU 门槛降低，更容易匹配到 XML 元素
`BBOX_CENTER_DIST_RATIO`	中心距容忍范围扩大近 2 倍
`BBOX_AREA_RATIO_MIN`	允许匹配更小的元素
`BBOX_AREA_RATIO_MAX`	允许匹配更大的元素

#### 问题3
对于广告弹窗的处理
#### 修改内容
- `build_explorer_prompt()` 新增第 11 条：要求模型检测广告/弹窗并返回 `popup: {detected, close_point}` 字段，坐标格式 0-1000
- `call_explorer_model()` 新增解析 `popup_info` 并作为第二返回值
- `explore_dfs()` 新增弹窗自动关闭循环：有 `close_point` 则点击，否则按返回键；关闭后重新调用 Explorer 验证，最多重试 `popup_dismiss_max_attempts` 次（默认 2，可通过 bat 参数配置）
1、点击关闭
2、等待稳定
3、重新explorer检查是否还有广告
4、若有再重试

#### 新问题 广告类型多种多样，可能有的必须要等待一定时间在解决


#### 问题4
对于不同页面的判断，比如美团的待付款 待收款 多个选项界面非常相似，且会陷入循环 回溯后同一个界面重新进入后选项变了
#### 修改内容
回溯验证三重指纹中的文本指纹（`fp_ok`）改用稳定文本指纹：
- 新增 `_stable_text_fingerprint()`：只提取 resource-id 或 class 包含 `title/tab/nav/toolbar/header/bottom/action_bar` 等关键词的固定 UI 元素文字计算 SHA1，忽略推荐内容、商家名称等动态文字
- 提取不到稳定元素时自动退化为原全文本指纹 `_hierarchy_fingerprint()`
- 替换回溯验证（第 2232 行）和全路径重播验证（第 2270 行）中的 `fp_ok` / `r_fp_ok` 计算
- 效果：动态 feed 页面（美团首页等）回溯后内容刷新不再导致 `fp_ok = False` 误判；待付款/待收货等 Tab 因标题文字不同仍能正确区分


#### 问题5
一些加载页面，模型立刻判断检测，导致问题
#### 修改内容
动作执行后新增两段式等待逻辑（`_wait_for_page_loaded`）：
1. 先固定等待 `PAGE_LOAD_WAIT_SEC`（默认 1.5s），让页面开始渲染
2. 再循环最多 `PAGE_LOAD_STABLE_MAX_POLLS` 次（默认 6 次，每次间隔 0.5s），将截图发给 VLM 判断页面是否仍处于加载状态（spinner、骨架屏、空白内容区、"加载中"文字等）；VLM 回答 NO 或调用失败时立即退出循环，不阻塞流程
两个参数均可在 bat 文件中配置，适配不同 App 和手机性能


### 4.13


####
代码修改：
#####
修改一：回溯改成先按back回溯，再验证是否回溯成功，如果成功则不全路径重播，如果失败则重播

修改二：对于加载页面的处理
改回原来的 输出wait等待 循环让decider做决定

修改三：对于广告的处理
- **Layer 1（无 LLM）**：在 Explorer 调用前，用 `_find_dismissible_element()` 扫描 hierarchy，匹配 resource-id/text 中含 `close/dismiss/skip/关闭/跳过` 等关键词的 clickable 元素，命中则直接点击关闭，无需 API 调用
- **倒计时广告**：Explorer 检测到弹窗但找不到关闭按钮时，用 `_detect_countdown()` 对比两次 hierarchy 快照（间隔 1s），若存在数值减少 1 的纯数字文本则判定为倒计时广告，等待其归零后重新检测关闭按钮；否则才按 back

对于广告的处理不加入树中

修改四：增加异步计算优化

**文件**：`MobiAgent/runner/mobiagent/auto-search.py`

将以下操作改为后台线程 fire-and-forget，不再阻塞 DFS 主流程：
- **图像标注**（`annotate_action_visuals`）：新增 `_run_annotation_safe` 包装，通过模块级 `_ANNOTATION_EXECUTOR`（ThreadPoolExecutor, max_workers=2）异步执行，每步节省 1-2s
- **hierarchy 写盘**（`save_hierarchy`）：新增 `_write_hierarchy_safe` 包装，设备 dump 同步完成后将文件写入提交给后台线程，每步节省 0.3-0.5s
- **JSON 持久化**（`persist_step_output` / `persist_outputs`）：新增 `_persist_step_output_safe` / `_persist_outputs_safe` 包装，每条路径完成写盘不阻塞回溯

新增 `_compute_fingerprints_concurrent`（+ `_safe_future`），利用 ThreadPoolExecutor 并发计算三重指纹（`_hierarchy_fingerprint`、`_compute_hierarchy_struct_fingerprint`、`_compute_dhash_hex`），替换以下四处串行计算：
- `enqueue_ui_collect_if_new`：fp + struct_fp 并发
- `explore_dfs` DFS 入口：fp + struct_fp 并发
- `explore_dfs` 候选执行前基准采集：struct_fp + dhash 并发
- 回溯三重验证 + 全路径重播验证：struct_fp + dhash 并发

新增 `--ui_collect_num_workers`（默认 1，可设为 2-4），支持 UI 采集任务队列多线程并发处理；`finally` 块按 worker 数量发送对应数量的 stop sentinel 并逐一 join。

自适应广度和深度探索
#####


#### 测试：
https://arxiv.org/abs/2412.19723
OS-Genesis: Automating GUI Agent Trajectory Construction via Reverse Task Synthesis

指标：
数据规模：
排除掉由于模型幻觉、环境崩溃导致的失败路径后，真正能用的数据量
可覆盖 UI 页面/App 数
Scaling 曲线：喂给模型的数据越多，模型的成功率就越高

运行效率：
设备小时数
API 调用次数与缓存
回溯的成功率

数据质量：
Trajectory Reward Model 对数据进行评分 增加对于语义连贯性的打分
指令多样性 不能全是点击 可以统计一下最后数据中涵盖各种操作的数量
人工 vs 自动的相关性 证明trm与人工打分的一致性

回溯准确性的提升

#### 论文整体思路：

**核心贡献**：已有成果研究大多数基于无监督随机遍历收集数据，移动 GUI agent 的训练数据瓶颈在于"规模、语义、可靠性"三者不可兼得——人工标注昂贵不可扩展，随机探索语义无意义，单 LLM 任务驱动局部合理但全局不连贯且回溯不可靠。AutoSearch 通过双模型 DFS 架构同时解决这三个问题。

---

#### 1. 核心总结 (Summary of Findings)

本文提出了 AutoSearch——一种面向移动端 GUI Agent 的自动化轨迹数据收集框架，将双模型架构与深度优先搜索（DFS）探索及可靠回溯机制相结合。我们在 N 款真实移动应用上的实验评估揭示了以下核心结论。

**规模与效率。** AutoSearch 在 Z 款 App 上累计收集了 X 条有效多步操作轨迹，覆盖 Y 个独立 UI 页面，平均收集吞吐量约为 W 条轨迹/设备小时。排除因模型幻觉或环境崩溃而失效的路径后，有效轨迹率达到 XX%，优于随机爬取基线方法的 YY%。Explorer 响应缓存（OPT-9）在回溯返回已访问页面时减少了 XX% 的冗余 API 调用；设备状态缓存（OPT-10）减少了 XX% 的重复 ADB 设备交互。

**回溯可靠性。** 我们提出的三重指纹验证机制——结合稳定文本指纹、结构指纹（仅 clickable 元素）与视觉 dHash——回溯准确率达到 XX%，较单一文本指纹验证（YY%）显著提升。在美团首页等动态内容页面上，稳定文本指纹策略在 XX% 的情况下正确区分了结构相同、内容不同的页面，而朴素全文本指纹在这些场景下失效。当单步回溯验证失败时，全路径重播恢复机制在两次重播尝试内成功恢复正确设备状态的概率为 XX%。

**数据质量与下游效果。** 基于少量人工标注轨迹训练的轨迹奖励模型（Trajectory Reward Model，TRM）对所有收集数据进行评分，TRM 评分与人工判断的 Pearson 相关系数达 r = 0.XX（p < 0.001），验证了 TRM 作为人工质量评估代理的可靠性。收集数据的指令分布覆盖各主要动作类型：点击（XX%）、滑动（XX%）、文字输入（XX%）、滚动（XX%），呈现良好的动作多样性，而非以单一点击操作为主。Scaling 实验表明，随训练轨迹数量从 1k 增长至 10k，下游 Decider 模型在 [Benchmark] 上的任务完成率从 XX% 单调提升至 XX%，证实了所收集数据对模型训练的实际价值。

---

#### 2. 贡献与意义 (Contributions & Implications)

本工作的核心贡献在于提出了一种原则性框架，同时解决了移动端 GUI Agent 数据收集的三大瓶颈——**规模、语义连贯性与轨迹可靠性**——而这三者在既有方法中难以兼顾。

**方法论贡献：Explorer-Decider 解耦架构。** 现有方法或依赖无监督随机遍历（数据量大但语义缺失），或采用单 LLM 任务驱动执行（局部连贯但缺乏可靠回溯）。OS-Genesis 引入了逆向任务合成思路，先随机收集 UI 状态，再由 LLM 事后推断任务描述。这种后验标注方法虽能生成多样化任务标签，但无法保证推断出的任务具有端到端可执行性，也未解决多步路径连贯性与回溯问题。AutoSearch 采取本质上不同的前向规划策略：Explorer 模型在执行前就生成具有明确语义意图的候选任务，Decider 模型仅在该语义范围内执行操作；三重指纹回溯机制进一步确保 DFS 树中所有兄弟候选始终从经过验证的同一父状态出发执行，避免因设备状态不确定而污染训练数据。

**工程贡献：大规模场景下的可靠回溯。** 结合动态内容页面稳定文本指纹与全路径重播恢复的回溯策略，是对移动端 UI 自动化探索中一个广为人知但缺乏系统解决的问题的实用回答：如何在执行 UI 动作后可靠地回到已知设备状态。据我们所知，本文首次将回溯准确率作为一级指标纳入移动端 GUI 轨迹收集的量化评估体系。

**实用价值：可持续的数据飞轮。** AutoSearch 直接作用于线上移动应用，无需手工指定任务或插桩 UI，因此每当 App 更新后可重新运行，自动收集覆盖新 UI 页面的轨迹数据。这构建了一条自维持的数据管线：随着应用生态的演进和新交互方式的出现，框架可持续为下游 Agent 模型补充最新训练数据，无需人工介入，形成与应用生态并行成长的数据飞轮。

**数据集贡献。** AutoSearch 收集的轨迹数据构成目前规模最大的中文移动端 GUI 操作数据集，涵盖电商、社交、导航、效率工具等 N 类应用共 Z 款 App。数据集包含逐步截图、UI 无障碍层级 Dump、动作可视化标注及 TRM 质量评分，可支持 SFT 训练以外的多种下游研究，包括 UI 语义理解、视觉定位与奖励建模。

---

#### 3. 局限与展望 (Limitations & Future Work)

**局限性。** 尽管 AutoSearch 在当前评估场景中取得了较强结果，仍存在若干有待解决的局限。

其一，**广告与弹窗处理尚不完备**。当前实现通过 Explorer 模型输出的 `popup` 字段检测弹窗，并尝试点击关闭按钮或按返回键来消除遮挡。然而，部分广告形态——尤其是需等待固定倒计时后才出现跳过按钮的强制曝光广告——无法通过这种响应式策略处理，导致当前探索路径停滞甚至提前终止。

其二，**Explorer 模型依赖外部 API 调用**（OpenRouter）。大规模运行时，这带来了货币成本与网络延迟的双重约束。每个 DFS 节点至少需要一次 Explorer API 调用，尽管缓存机制（OPT-9）缓解了重访同一页面时的冗余调用，首次访问成本仍不可避免，同时也限制了框架在离线或低成本部署环境中的适用性。

其三，**探索的深度（D）与广度（B）为固定超参数**。当前设计对整个 App 使用全局统一的 D、B 值。对于导航结构密集的页面（如包含数十个选项的设置列表），低广度设置导致探索不足；而对于简单的过渡页面（如加载页、单按钮确认框），广度预算又存在浪费。

其四，**并发能力受限于单设备顺序执行**。DFS 探索由单台设备串行完成，这从物理上限制了面向大量 App 的大规模收集任务的吞吐量。

**未来工作。** 基于上述局限，我们认为以下研究方向具有较强的后续价值。

*多设备并行 DFS。* 多台物理设备或模拟器可同时执行独立的 DFS 分支，通过共享 Explorer 响应缓存与页面注册表来协调覆盖范围、避免重复探索。理论上，收集吞吐量可随设备数量线性扩展。

*自适应深度与广度调度。* 不再使用固定的 D、B 值，而是根据单页面信号动态调整探索力度：可交互元素数量、已探索候选占比，以及来自同页面历史路径的 TRM 评分。这将把有限的探索预算集中分配至高价值页面，同时对内容稀疏的过渡页面剪枝。

*在线 TRM 引导过滤。* 当前 TRM 评分仅在收集完成后离线应用。若将 TRM 评估集成到在线 DFS 循环中，可实时发现并丢弃低质量轨迹，减少在最终会被过滤掉的路径上的设备交互与 API 消耗，提升有效数据比例。

*本地化 Explorer 部署。* 以轻量本地部署的 VLM 替代基于 API 的 Explorer，可消除外部依赖、降低每个 DFS 节点的延迟，并支持完全离线的数据收集管线。候选模型包括可在消费级 GPU 上与 Decider 推理服务共同部署的量化 7B–13B VLM。



