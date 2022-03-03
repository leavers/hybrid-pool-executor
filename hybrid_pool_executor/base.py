import asyncio
from abc import ABC, abstractmethod
from concurrent.futures._base import CancelledError as BaseCancelledError
from concurrent.futures._base import Executor
from concurrent.futures._base import Future as _Future
from dataclasses import dataclass, field
from queue import SimpleQueue
from typing import Any, Dict, FrozenSet, Literal, Optional, Tuple, Type, Union, cast

from hybrid_pool_executor.constants import ACT_NONE, ActionFlag, Function

"""
For python 3.7+, there is no significant speed/size difference between object,
dataclass and namedtuple.
"""


@dataclass
class Action:
    """The base dataclass of action.

    Actions are objects used to transfer information between worker(s) and manager(s)
    through request/response queue.
    """

    flag: ActionFlag = ACT_NONE
    message: Optional[str] = None
    task_name: Optional[str] = None
    worker_name: Optional[str] = None
    result: Any = None
    exception: Optional[BaseException] = None

    def add_flag(self, flag: ActionFlag):
        self.flag |= flag

    def match(
        self,
        *flags: Union[Tuple[ActionFlag], ActionFlag],
        strategy: Literal["all", "any"] = "any",
    ) -> bool:
        if strategy not in ("any", "all"):
            raise ValueError(
                'Param "strategy" should be "any" or "all", '
                f'got "{strategy}" instread".'
            )
        if len(flags) == 1 and isinstance(flags[0], (tuple, list, set)):
            flags = flags[0]
        flags = cast(Tuple[ActionFlag, ...], flags)
        m = map(lambda flag: self.flag & flag, flags)
        return any(m) if strategy == "any" else all(m)


@dataclass
class BaseTask(ABC):
    """The base dataclass of task.

    Calls are objects used to carry functions to worker(s).

    BaseCall is regarded as a abstract class and should not be initialized directly.
    """

    name: str
    fn: Function
    args: Tuple[Any, ...] = ()
    kwargs: Dict[str, Any] = field(default_factory=dict)
    cancelled: bool = False


@dataclass
class BaseWorkerSpec(ABC):
    """The base dataclass of work specification.

    BaseWorkerSpec is regarded as a abstract class and should not be initialized
    directly.

    :param name: Name of worker.
    :type name: str

    :param task_bus: The queue for sending task item.
    :type task_bus: SimpleQueue

    :param request_bus: The queue for receiving requests from manager.
    :type request_bus: SimpleQueue

    :param response_bus: The queue for sending responses to manager.
    :type response_bus: SimpleQueue

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
    task_bus: SimpleQueue = field(default_factory=SimpleQueue)
    request_bus: SimpleQueue = field(default_factory=SimpleQueue)
    response_bus: SimpleQueue = field(default_factory=SimpleQueue)
    idle_timeout: float = 60.0
    wait_interval: float = 0.1
    max_task_count: int = 12
    max_err_count: int = 3
    max_cons_err_count: int = -1


class BaseWorker(ABC):
    @abstractmethod
    def __init__(self, spec: BaseWorkerSpec):
        pass

    @abstractmethod
    def start(self):
        pass

    @abstractmethod
    def is_idle(self) -> bool:
        pass

    @abstractmethod
    def stop(self):
        pass

    @abstractmethod
    def terminate(self):
        pass


class Future(_Future):
    def __init__(self):
        super().__init__()
        self._got: bool = False

        def cb(_):
            self._got = True

        self.add_done_callback(cb)

    async def _async_result(self):
        while not self._got:
            await asyncio.sleep(0.01)
        return self.result()

    def __await__(self):
        return self._async_result().__await__()


BaseExecutor = Executor
CancelledError = BaseCancelledError


@dataclass
class BaseManagerSpec(ABC):
    mode: str = "abstract"
    num_workers: int = -1
    incremental: bool = True
    wait_interval: float = 0.1
    name_pattern: str = "Manager-{manager}"
    worker_name_pattern: str = "Worker-{worker}"


class BaseManager(ABC):
    @abstractmethod
    def __init__(self, spec: BaseManagerSpec):
        pass

    @abstractmethod
    def start(self):
        pass

    @abstractmethod
    def is_alive(self) -> bool:
        pass

    @abstractmethod
    def submit(
        self,
        fn: Function,
        args: Optional[Tuple[Any, ...]] = (),
        kwargs: Optional[Dict[str, Any]] = None,
        name: Optional[str] = None,
    ) -> Future:
        pass

    @abstractmethod
    def stop(self, timeout: Optional[float] = None):
        pass

    @abstractmethod
    def terminate(self):
        pass

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


@dataclass
class ModuleSpec:
    name: str
    manager_class: Type[BaseManager]
    manager_spec_class: Type[BaseManagerSpec]
    worker_class: Type[BaseWorker]
    worker_spec_class: Type[BaseWorkerSpec]
    tags: FrozenSet[str]
    enabled: bool = True
