import typing as t
from queue import Queue, SimpleQueue

T_co = t.TypeVar("T_co", covariant=True)


class QueueLike(t.Protocol[T_co]):
    def get(self, block=True, timeout=None, **kwargs) -> T_co:
        ...

    def put(self, obj, block=True, timeout=None, **kwargs) -> T_co:
        ...

    def empty(self) -> bool:
        ...

    def qsize(self) -> int:
        ...


# Function = t.Union[t.Callable[..., t.Any], t.Coroutine[t.Any, t.Any, t.Any]]
ThreadBus = t.Union[Queue, SimpleQueue]
ThreadBusType = t.Union[t.Type[Queue], t.Type[SimpleQueue], t.Callable[[], ThreadBus]]
ProcessBus = QueueLike
ProcessBusType = t.Union[t.Type[ProcessBus], t.Callable[[], ProcessBus]]

PRESERVED_TASK_TAGS = frozenset({"async", "process", "thread"})

ActionFlag = int
ACT_NONE = 0
ACT_DONE = 1
ACT_CLOSE = 1 << 1
ACT_EXCEPTION = 1 << 2
ACT_RESTART = 1 << 3
ACT_RESET = 1 << 4
ACT_FATAL_ERROR = 1 << 6
ACT_TIMEOUT = 1 << 6
ACT_CANCEL = 1 << 7
ACT_COERCE = 1 << 8
