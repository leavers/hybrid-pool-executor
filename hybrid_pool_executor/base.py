from abc import ABC
from concurrent.futures._base import Future as _Future
from dataclasses import dataclass, field
from queue import SimpleQueue
from typing import Any, Callable, Dict, Hashable, Optional, Tuple

from hybrid_pool_executor.typing import (
    ActionFlag,
    ACT_NONE,
    WorkerMode,
    WORKER_MODE_THREAD,
)

"""
For python 3.7+, there is no significant speed/size difference between object,
dataclass and namedtuple.
"""


@dataclass
class WorkerSpec(ABC):
    """The base dataclass of work specification.

    WorkerSpec is regarded as a abstract class and should not be initialized directly.

    :param name: Name of worker.
    :type name: Hashable

    :param work_queue: The queue for sending task item.
    :type work_queue: SimpleQueue

    :param request_queue: The queue for receiving requests from manager.
    :type request_queue: SimpleQueue

    :param response_queue: The queue for sending responses to manager.
    :type response_queue: SimpleQueue

    :param daemon: True if worker should be a daemon, defaults to True.
    :type daemon: bool, optional

    :param idle_timeout: Second(s) before the worker should exit after being idle,
        defaults to 60.
    :type idle_timeout: float, optional

    :param wait_interval: Interval in second(s) the worker fetches information,
        defaults to 0.1.
    :type wait_interval: float, optional

    :param max_task_count: Maximum task amount the worker can process, after that the
        worker should be destroyed, defaults to 12, negative value means unlimited.
    :type max_task_count: int, optional

    :param max_err_count: Maximum error amount the worker can afford, after that the
        worker should be destroyed, defaults to 3, negative value means unlimited.
    :type max_err_count: int, optional

    :param max_cons_err_count: Maximum continuous error amount the worker can afford,
        after that the worker should be destroyed, defaults to -1, negative value means
        unlimited.
    :type max_cons_err_count: int, optional
    """

    name: Hashable
    task_queue: SimpleQueue
    request_queue: SimpleQueue
    response_queue: SimpleQueue
    daemon: bool = True
    idle_timeout: float = 60.0
    wait_interval: float = 0.1
    max_task_count: int = 12
    max_err_count: int = 3
    max_cons_err_count: int = -1


@dataclass
class Action(ABC):
    """The base dataclass of action.

    Actions are objects used to transfer information between worker(s) and manager(s)
    through request/response queue.

    Action is regarded as a abstract class and should not be initialized directly.
    """

    flag: ActionFlag = ACT_NONE
    message: Optional[str] = None
    task_id: Hashable = None
    worker_id: Hashable = None
    worker_mode: WorkerMode = WORKER_MODE_THREAD
    result: Any = None
    exception: Optional[BaseException] = None

    def add_flag(self, flag: ActionFlag):
        self.flag |= flag

    def match(self, *flags) -> bool:
        if len(flags) == 1 and isinstance(flags[0], (tuple, list, set)):
            flags = flags[0]
        for flag in flags:
            if self.flag & flag:
                return True
        return False


@dataclass
class Call(ABC):
    """The base dataclass of call item.

    Calls are objects used to carry functions to worker(s).

    Call is regarded as a abstract class and should not be initialized directly.
    """

    name: Hashable
    func: Callable
    args: Tuple[Any, ...] = ()
    kwargs: Dict[str, Any] = field(default_factory=dict)
    work_mode: WorkerMode = WORKER_MODE_THREAD
    cancelled: bool = False


class Future(_Future):
    pass
