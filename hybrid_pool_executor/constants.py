import typing as t
from multiprocessing.queues import Queue as ProcessQueue
from queue import Queue, SimpleQueue

Function = t.Union[t.Callable[..., t.Any], t.Coroutine[t.Any, t.Any, t.Any]]
ThreadBus = t.Union[Queue, SimpleQueue]
ThreadBusType = t.Union[t.Type[Queue], t.Type[SimpleQueue], t.Callable[[], ThreadBus]]
ProcessBus = ProcessQueue
ProcessBusType = t.Union[t.Type[ProcessBus], t.Callable[[], ProcessBus]]

PRESERVED_TASK_TAGS = frozenset({"async", "process", "thread"})

ActionFlag = int
ACT_NONE = 0
ACT_DONE = 1
ACT_CLOSE = 1 << 1
ACT_EXCEPTION = 1 << 2
ACT_RESTART = 1 << 3
ACT_RESET = 1 << 4
ACT_TIMEOUT = 1 << 5
ACT_CANCEL = 1 << 6
ACT_COERCE = 1 << 7
