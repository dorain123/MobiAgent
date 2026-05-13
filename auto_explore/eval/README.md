# Auto Explore Eval

`auto_explore` 的评分端建议按“读取轨迹样本 -> 调用评审模型 -> 输出结构化评分 JSON”来做，而不是只输入一段裸 `reasoning`。

这样做的原因：

- 轨迹完整性必须结合 `actions.json` 和 `react.json` 一起看，单看 `reasoning` 容易误判。
- 质量分最好拆成两大主分：
  - `trajectory_completeness_score`
  - `reasoning_quality_score`
- 模型输出除了分数，还要带简短优点、问题和总结，后续更方便做人工复核和实验对比。

源码位置：

- `auto_explore/src/auto_explore/eval/loader.py`
- `auto_explore/src/auto_explore/eval/judge.py`
- `auto_explore/src/auto_explore/eval/cli.py`

推荐运行方式：

```bash
PYTHONPATH=auto_explore/src python -m auto_explore.eval.cli \
  --input_path auto_explore/data/淘宝/20260420-213844 \
  --target_level paths \
  --judge_model <judge_model>
```

默认环境变量优先级：

- `AUTO_EXPLORE_EVAL_API_KEY`
- `SJTU_API_KEY`
- `OPENROUTER_API_KEY`

默认 `base_url` 规则：

- 若设置了 `AUTO_EXPLORE_EVAL_BASE_URL`，优先使用它
- 否则如果检测到 `SJTU_API_KEY`，默认使用 `https://models.sjtu.edu.cn/api/v1`
- 再否则回退到 `https://openrouter.ai/api/v1`

默认输出会写到：

```text
auto_explore/eval/results/<timestamp>/summary.json
```

Windows wrapper:

```bat
set SJTU_API_KEY=your-sjtu-key
auto_explore\scripts\run_eval.bat
```

Optional overrides:

- `AUTO_EXPLORE_EVAL_INPUT_PATH`
- `AUTO_EXPLORE_EVAL_OUTPUT_PATH`
- `AUTO_EXPLORE_EVAL_TARGET_LEVEL`
- `AUTO_EXPLORE_EVAL_MODEL`
- `AUTO_EXPLORE_EVAL_BASE_URL`
- `AUTO_EXPLORE_EVAL_API_KEY`
- `AUTO_EXPLORE_EVAL_JUDGE_MODE`

## Path Multimodal Mode

Use `--judge_mode path_multimodal` when the input contains real `path_*` samples with numbered screenshots such as `1.jpg`, `2.jpg`, ... inside each path directory.

Example:

```bash
PYTHONPATH=auto_explore/src python -m auto_explore.eval.cli \
  --input_path auto_explore/results/ablation/20260423_151917_淘宝/E0_full/run_001 \
  --target_level paths \
  --judge_mode path_multimodal \
  --judge_model qwen3vl
```

Notes:

- `path_multimodal` only supports `path_*` samples.
- It does not silently downgrade to `steps/`.
- Each step sends the original numbered screenshot and, when present, the matching `N_click_point.jpg`.
- The summary averages `trajectory_completeness_score`, `image_coherence_score`, and `task_operation_match_score`.
- If evaluation is interrupted, a partial summary is written with `interrupted`, `planned_sample_count`, `evaluated_sample_count`, and `remaining_sample_count`.
