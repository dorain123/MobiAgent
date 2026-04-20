import concurrent.futures


API_TIMEOUT = 45
EXPLORER_MAX_TOKENS = 1024
MAX_RETRIES = 3
DEVICE_WAIT_TIME = 0.6
DECIDER_MODEL_PLACEHOLDER = ""

_ANNOTATION_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=2)
_FP_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=3)


class WaitActionSkip(Exception):
    """Decider 返回 wait 动作时抛出，通知 DFS 跳过本候选、不计入步骤。"""


PAGE_LOAD_WAIT_SEC: float = 1.5
PAGE_LOAD_STABLE_MAX_POLLS: int = 6
PAGE_LOAD_STABLE_POLL_INTERVAL: float = 0.5

BBOX_REFINE_IOU_THRESHOLD = 0.3
BBOX_REFINE_CENTER_DIST_RATIO = 0.08
BBOX_REFINE_AREA_RATIO_MIN = 0.5
BBOX_REFINE_AREA_RATIO_MAX = 2.0
