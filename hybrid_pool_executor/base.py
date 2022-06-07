import typing as t
from abc import ABC, abstractmethod
from concurrent.futures._base import CancelledError as BaseCancelledError
from concurrent.futures._base import Executor
from concurrent.futures._base import Future as BaseFuture
from dataclasses import dataclass, field

from hybrid_pool_executor.constants import ACT_NONE, ActionFlag, Function
from hybrid_pool_executor.utils import get_event_loop

"""
For python 3.7+, there is no significant speed/size difference between object,
dataclass and namedtuple.
"""

# TODO: migrate common codes into base.py
# TODO: discard exception traceback related to workers


@dataclass
class Action:
    """The base dataclass of action.

    Actions are objects used to transfer information between worker(s) and manager(s)
    through request/response queue.
    """

    flag: ActionFlag = ACT_NONE
    message: t.Optional[str] = None
    task_name: t.Optional[str] = None
    worker_name: t.Optional[str] = None
    result: t.Any = None
    exception: t.Optional[BaseException] = None

    def add_flag(self, flag: ActionFlag):
        self.flag |= flag

    def match(
        self,
        *flags: t.Union[t.Iterable[ActionFlag], ActionFlag],
        strategy: t.Literal["all", "any"] = "any",
    ) -> bool:
        if strategy not in ("any", "all"):
            raise ValueError(
                'Param "strategy" should be "any" or "all", '
                f'got "{strategy}" instread".'
            )
        if len(flags) == 1 and isinstance(flags[0], (tuple, list, set)):
            flags = tuple(flags[0])
        flags = t.cast(t.Tuple[ActionFlag, ...], flags)
        m = map(lambda flag: self.flag & flag, flags)
        return any(m) if strategy == "any" else all(m)


@dataclass
class WorkerState:
    """A dataclass which stores running state of worker.

    A bare bool flag may not be synced in concurrent situation, this is why we need
    a WorkerState class.
    """

    inited: bool = False
    running: bool = False
    idle: bool = False


@dataclass
class BaseTask(ABC):
    """The base dataclass of task.

    Calls are objects used to carry functions to worker(s).

    BaseCall is regarded as a abstract class and should not be initialized directly.
    """

    name: str
    fn: Function
    args: t.Iterable[t.Any] = ()
    kwargs: t.Dict[str, t.Any] = field(default_factory=dict)
    cancelled: bool = False


@dataclass
class BaseWorkerSpec(ABC):
    """The base dataclass of worker specification.

    BaseWorkerSpec is regarded as a abstract class and should not be initialized
    directly.

    :param name: Name of worker.
    :type name: str

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
    idle_timeout: float = 60.0
    wait_interval: float = 0.1
    max_task_count: int = 12
    max_err_count: int = 3
    max_cons_err_count: int = -1


class BaseWorker(ABC):
    def __init__(self, *args, **kwargs):
        self._state = WorkerState()

    def is_alive(self) -> bool:
        return self._state.running

    def is_idle(self) -> bool:
        return self._state.idle if self._state.running else False

    def _ensure_running(self) -> None:
        if not self._state.running:
            raise RuntimeError(
                f"{self.__class__.__name__} is either stopped or not started "
                "yet and not able to accept tasks."
            )

    @abstractmethod
    def start(self):
        ...

    @abstractmethod
    def stop(self, timeout: t.Optional[float] = None):
        ...

    @abstractmethod
    def terminate(self):
        ...

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


class Future(BaseFuture):
    def __init__(self):
        super().__init__()
        self._loop = get_event_loop()
        self._async_fut = self._loop.create_future()

        def cb(_):
            self._async_fut.get_loop().call_soon_threadsafe(
                self._async_fut.set_result, True
            )

        self.add_done_callback(cb)

    async def _async_result(self):
        await self._async_fut
        return self.result()

    def __await__(self):
        return self._async_result().__await__()


BaseExecutor = Executor
CancelledError = BaseCancelledError


class ExistsError(Exception):
    ...


class NotSupportedError(Exception):
    ...


@dataclass
class BaseManagerSpec(ABC):
    name_pattern: str = "BaseManager-{manager_seq}"
    worker_name_pattern: str = "BaseWorker-{worker_seq} [{manager}]"
    task_name_pattern: str = "BaseTask-{task_seq} [{manager}]"
    num_workers: int = -1
    incremental: bool = True
    wait_interval: float = 0.1
    idle_timeout: float = 60.0


class BaseManager(BaseWorker):
    @t.overload
    def submit(
        self,
        fn: t.Coroutine[t.Any, t.Any, t.Any],
        *,
        name: t.Optional[str] = None,
    ) -> Future:
        ...

    @t.overload
    def submit(
        self,
        fn: t.Callable[..., t.Any],
        args: t.Optional[t.Iterable[t.Any]] = (),
        kwargs: t.Optional[t.Dict[str, t.Any]] = None,
        name: t.Optional[str] = None,
    ) -> Future:
        ...

    @abstractmethod
    def submit(
        self,
        fn: Function,
        args: t.Optional[t.Iterable[t.Any]] = (),
        kwargs: t.Optional[t.Dict[str, t.Any]] = None,
        name: t.Optional[str] = None,
    ) -> Future:
        ...


def adjust_worker_iterator(
    spec: BaseManagerSpec,
    curr_workers: t.Iterable[BaseWorker],
    num_curr_tasks: int,
) -> range:
    if spec.incremental or spec.num_workers < 0:
        num_idle_workers: int = sum(1 if w.is_idle() else 0 for w in curr_workers)
        if spec.num_workers < 0:
            iterator = range(num_curr_tasks - num_idle_workers)
        else:
            num_curr_workers: int = len(t.cast(t.Sized, curr_workers))
            iterator = range(
                num_curr_workers,
                min(
                    spec.num_workers,
                    num_curr_workers + num_curr_tasks - num_idle_workers,
                ),
            )
    else:
        iterator = range(len(t.cast(t.Sized, curr_workers)), spec.num_workers)
    return iterator


@dataclass
class ModuleSpec:
    name: str
    manager_type: t.Union[
        t.Type[BaseManager],
        t.Callable[[BaseManagerSpec], BaseManager],
    ]
    manager_spec_type: t.Union[
        t.Type[BaseManagerSpec],
        t.Callable[[], BaseManagerSpec],
    ]
    worker_type: t.Union[
        t.Type[BaseWorker],
        t.Callable[[BaseWorkerSpec], BaseWorker],
    ]
    worker_spec_type: t.Union[
        t.Type[BaseWorkerSpec],
        t.Callable[[], BaseWorkerSpec],
    ]
    tags: t.FrozenSet[str]
    enabled: bool = True

    def __post_init__(self):
        assert self.name in self.tags
