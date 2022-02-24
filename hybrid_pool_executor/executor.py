from dataclasses import dataclass
from typing import Dict, Hashable, Optional, Tuple


@dataclass
class ExecutorModeSpec:
    mode: str
    enabled: bool = True
    num_workers: int = 0
    incremental: bool = True
    worker_name_pattern: str = "Worker-{}"


class HybridPoolExecutor:
    def __init__(
        self,
        thread_workers: int = 0,
        incremental_thread_pool: bool = True,
        redirect_thread: Optional[str] = None,
        thread_name_pattern: Optional[str] = None,
        **kwargs,
    ):
        pass
