from abc import ABC, abstractmethod, abstractclassmethod
from concurrent.futures._base import Executor
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
class BaseWorkerSpec(ABC):
    """The base dataclass of work specification.

    BaseWorkerSpec is regarded as a abstract class and should not be initialized directly.

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
    task_bus: SimpleQueue
    request_bus: SimpleQueue
    response_bus: SimpleQueue
    daemon: bool = True
    idle_timeout: float = 60.0
    wait_interval: float = 0.1
    max_task_count: int = 12
    max_err_count: int = 3
    max_cons_err_count: int = -1


@dataclass
class BaseAction(ABC):
    """The base dataclass of action.

    Actions are objects used to transfer information between worker(s) and manager(s)
    through request/response queue.

    BaseAction is regarded as a abstract class and should not be initialized directly.
    """

    flag: ActionFlag = ACT_NONE
    message: Optional[str] = None
    task_id: Optional[Hashable] = None
    worker_id: Optional[Hashable] = None
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
class BaseCall(ABC):
    """The base dataclass of call item.

    Calls are objects used to carry functions to worker(s).

    BaseCall is regarded as a abstract class and should not be initialized directly.
    """

    name: Hashable
    func: Callable
    args: Tuple[Any, ...] = ()
    kwargs: Dict[str, Any] = field(default_factory=dict)
    work_mode: WorkerMode = WORKER_MODE_THREAD
    cancelled: bool = False


class BaseAsyncFutureInterface(ABC):
    @abstractclassmethod
    async def cancel(cls, *args, **kwargs) -> bool:
        pass

    @abstractclassmethod
    async def result(cls, *args, **kwargs) -> bool:
        pass

    @abstractclassmethod
    async def set_result(cls, *args, **kwargs):
        pass

    @abstractclassmethod
    async def set_exception(cls, *args, **kwargs):
        pass


class BaseFuture(_Future, ABC):
    @abstractmethod
    def cancel(self) -> bool:
        pass

    @abstractmethod
    def cancelled(self) -> bool:
        pass

    @abstractmethod
    def running(self) -> bool:
        pass

    @abstractmethod
    def done(self) -> bool:
        pass

    @abstractmethod
    def add_done_callback(self, fn: Callable[["BaseFuture"], Any]):
        pass

    @abstractmethod
    def result(self, timeout: Optional[float] = None):
        pass

    @abstractmethod
    def set_running_or_notify_cancel(self):
        pass

    @abstractmethod
    def set_result(self, result: Any):
        pass

    @abstractmethod
    def set_exception(self, exception: Exception):
        pass


class BaseWorker(ABC):
    @abstractmethod
    def start(self):
        pass

    @abstractmethod
    def run(self):
        pass

    @abstractmethod
    def stop(self):
        pass

    @abstractmethod
    def idle(self):
        pass


BaseExecutor = Executor
