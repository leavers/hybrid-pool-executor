from abc import ABC, abstractmethod, abstractclassmethod
from concurrent.futures._base import Executor
from concurrent.futures._base import Future
from dataclasses import dataclass, field
from queue import SimpleQueue
from typing import Any, Callable, Coroutine, Dict, Optional, Tuple, Type, Union
from hybrid_pool_executor.typing import ActionFlag, ACT_NONE, Function


class CancelledError(Exception):
    pass


"""
For python 3.7+, there is no significant speed/size difference between object,
dataclass and namedtuple.
"""


@dataclass
class BaseAction(ABC):
    """The base dataclass of action.

    Actions are objects used to transfer information between worker(s) and manager(s)
    through request/response queue.

    BaseAction is regarded as a abstract class and should not be initialized directly.
    """

    flag: ActionFlag = ACT_NONE
    message: Optional[str] = None
    task_id: Optional[str] = None
    worker_id: Optional[str] = None
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


@dataclass
class BaseTask(ABC):
    """The base dataclass of task.

    Calls are objects used to carry functions to worker(s).

    BaseCall is regarded as a abstract class and should not be initialized directly.
    """

    name: str
    func: Function
    args: Tuple[Any, ...] = ()
    kwargs: Dict[str, Any] = field(default_factory=dict)
    cancelled: bool = False


@dataclass
class BaseWorkerSpec(ABC):
    """The base dataclass of work specification.

    BaseWorkerSpec is regarded as a abstract class and should not be initialized directly.

    :param name: Name of worker.
    :type name: str

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

    name: str
    task_bus: SimpleQueue
    request_bus: SimpleQueue
    response_bus: SimpleQueue
    idle_timeout: float = 60.0
    wait_interval: float = 0.1
    max_task_count: int = 12
    max_err_count: int = 3
    max_cons_err_count: int = -1


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


@dataclass
class BaseManagerSpec(ABC):
    mode: str
    num_workers: int = -1
    incremental: bool = True
    name_pattern: str = "Manager-{manager}"
    worker_name_pattern: str = "Worker-{worker}"
    worker_daemon: bool = True
    worker_idle_timeout: float = 60.0
    worker_wait_interval: float = 0.1
    worker_max_task_count: int = 12
    worker_max_err_count: int = 3
    worker_max_cons_err_count: int = -1


class BaseManager(ABC):
    pass


BaseFuture = Future
BaseExecutor = Executor


@dataclass
class ModuleSpec:
    name: str
    manager_class: Type[BaseManager]
    manager_spec_class: Type[BaseManagerSpec]
    worker_class: Type[BaseWorker]
    worker_spec_class: Type[BaseWorkerSpec]
