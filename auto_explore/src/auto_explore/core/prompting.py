from typing import Any, Dict, List, Optional

from prompts.decider_qwen3_e2e import (
    DECIDER_CURRENT_STEP_PROMPT as AUTO_DECIDER_CURRENT_STEP_PROMPT,
    DECIDER_SYSTEM_PROMPT as AUTO_DECIDER_SYSTEM_PROMPT,
    DECIDER_USER_PROMPT as AUTO_DECIDER_USER_PROMPT,
)


def format_action_history(action_history: List[Dict[str, Any]], max_items: int = 20) -> str:
    if not action_history:
        return "无"

    recent = action_history[-max_items:]
    lines = []
    for i, item in enumerate(recent, 1):
        task = str(item.get("source_task", "")).strip()
        action_type = str(item.get("type", "")).strip()
        detail_parts = []
        if action_type in {"input", "click_input"} and item.get("text"):
            detail_parts.append(f"text={item.get('text')}")
        if action_type == "swipe" and item.get("direction"):
            detail_parts.append(f"direction={item.get('direction')}")
        detail = f" ({', '.join(detail_parts)})" if detail_parts else ""
        lines.append(f"{i}. task={task or '-'} | action={action_type or '-'}{detail}")
    return "\n".join(lines)


def build_explorer_prompt(
    depth: int,
    breadth: int,
    hierarchy_text: str,
    action_history: List[Dict[str, Any]],
    already_explored: Optional[List[str]] = None,
) -> str:
    """构建通用大模型的候选动作生成提示词。"""
    history_text = format_action_history(action_history)
    already_explored_text = ""
    if already_explored:
        items = "\n".join(f"- {t}" for t in already_explored)
        already_explored_text = f"\n已在当前页面完成探索的操作（请勿重复生成）:\n{items}\n"
    repeat_guard_text = """
Additional hard constraints:
- Do not repeat a task that is already completed in the action history.
- Do not repeat a task that is already listed as explored on the current page.
- If a navigation/tab/icon entry is already selected, or the target page is already reached, propose the next unexplored action on the current page instead of repeating the same navigation task.
- Do not output a bottom navigation/tab switching task if it only returns to the page the user is already on.
""".strip()
    return f"""
你是移动端GUI探索助手。请结合截图、层级信息以及已发生的交互动作序列，输出当前界面"最有可能被用户下一步操作"的前{breadth}个单步任务，优先选择左侧、顶部或者底部的导航栏中的元素，并尽可能保证前后动作的连贯性。尽量不选择返回按钮和重复的动作。如果只剩下返回按钮，则终止这条收集。

要求：
1) 只输出一个 JSON object，不要输出任何额外文本、解释、前后缀、markdown 或 ```json 代码块。
2) 输出字段必须是：
{{
  "candidates": [
    {{
      "rank": 1,
      "single_step_task": "一句话单步任务，例如：点击"搜索框"并输入"咖啡"",
      "reason": "为什么这个动作高概率"
    }}
  ]
}}
3) candidates 数量 <= {breadth}，按概率从高到低排序；如果确实无法确定候选，也必须返回合法 JSON，例如 {{"candidates": []}}。
4) single_step_task 必须可执行、原子化（单步），避免多步串联。
5) reason 必须短句化，尽量控制在 40 个字以内，不要写成长段解释。
6) 如果是点击输入框的动作，务必跟上合理的当前界面下的输入文本，例如：点击"搜索框"并输入"咖啡"。
7) 若界面左侧、底部或者顶部侧边栏存在导航列表项（例如设置列表，菜单列表等），请你优先输出对各个列表项的点击操作，如果当前界面显示的列表项不全，可以输出滑动操作以查看更多列表项。
8) 候选动作需要与最近的交互动作序列保持连贯，避免与已发生动作明显冲突；必要时可继续完成上一动作的后续步骤。
9) 如果界面的右下角有"我的"、"个人中心"之类的入口图标，并且该图标没有被选中（图标是实心或者如表下面有下滑杠，表示选中），建议优先输出点击该入口的动作。
10) 【严禁】不得生成任何点击返回按钮、返回箭头、左上角"<"图标、后退、回退的候选动作。这类动作会破坏探索路径的连贯性，即使界面上有返回按钮也不要选它。唯一例外：当前界面完全没有其他任何可交互元素时，才可以输出返回操作。
11) 最近已经交互过的动作，请不要重复执行了，除非当前界面没有其他明显的可交互元素了。
12) 【广告/弹窗检测】仔细观察截图，判断当前界面是否被广告、活动推广、权限请求、新手引导等弹窗遮挡了主界面内容。
- 如果有弹窗遮挡主界面：在 JSON 中额外添加 "popup" 字段：{{"detected": true, "close_point": [x, y]}}，close_point 是关闭/跳过按钮的中心坐标，使用 0-1000 相对坐标格式（与图片宽高对应）。如果找不到关闭按钮，只返回 {{"detected": true}}。
- 如果没有弹窗遮挡：不需要 popup 字段。
{already_explored_text}
当前探索深度: {depth}
当前路径动作序列(按时间顺序，最多展示20条):
{history_text}
{repeat_guard_text}
""".strip()


def _load_auto_decider_system_prompt() -> str:
    return AUTO_DECIDER_SYSTEM_PROMPT.strip()


def build_auto_decider_messages(task: str, history: List[str], screenshot_b64: str) -> List[Dict[str, Any]]:
    if history:
        history_str = "\n".join(f"{idx}. {h}" for idx, h in enumerate(history, 1))
    else:
        history_str = "(No history)"

    context_text = AUTO_DECIDER_USER_PROMPT.format(task=task, history=history_str)
    system_prompt = _load_auto_decider_system_prompt()
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": context_text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"}},
                {"type": "text", "text": AUTO_DECIDER_CURRENT_STEP_PROMPT},
            ],
        },
    ]


def append_done_to_path(
    path_actions: List[Dict[str, Any]],
    path_reacts: List[Dict[str, Any]],
    path_task_description: str,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    actions_with_done = [dict(item) for item in path_actions]
    reacts_with_done = [dict(item) for item in path_reacts]

    actions_with_done.append({"type": "done", "status": "success"})
    done_react = {
        "reasoning": (
            f"当前任务为：{path_task_description}。"
            "该任务执行完成，任务结束。"
        ),
        "function": {"name": "done", "parameters": {"status": "success"}},
    }
    reacts_with_done.append(done_react)
    return actions_with_done, reacts_with_done


__all__ = [
    "AUTO_DECIDER_CURRENT_STEP_PROMPT",
    "AUTO_DECIDER_SYSTEM_PROMPT",
    "AUTO_DECIDER_USER_PROMPT",
    "_load_auto_decider_system_prompt",
    "append_done_to_path",
    "build_auto_decider_messages",
    "build_explorer_prompt",
    "format_action_history",
]
