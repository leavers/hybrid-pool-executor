from typing import Literal

WorkerMode = Literal["thread", "process", "async", "local"]
ThreadFallbackMode = Literal["process", "local"]
ProcessFallbackMode = Literal["thread", "local"]
AsyncFallbackMode = Literal["thread", "local"]
WORKER_MODE_THREAD = "thread"
WORKER_MODE_PROCESS = "process"
WORKER_MODE_ASYNC = "async"
WORKER_MODE_LOCAL = "local"
WORKER_MODES = (
    WORKER_MODE_THREAD,
    WORKER_MODE_PROCESS,
    WORKER_MODE_ASYNC,
    WORKER_MODE_LOCAL,
)

ErrorMode = Literal["raise", "ignore", "coerce"]
ERROR_MODE_RAISE = "raise"
ERROR_MODE_IGNORE = "ignore"
ERROR_MODE_COERCE = "coerce"
ERROR_MODES = (ERROR_MODE_RAISE, ERROR_MODE_IGNORE, ERROR_MODE_COERCE)

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
ACT_CMD_WORKER_STOP_FLAGS = (ACT_CLOSE, ACT_RESTART)  # used for manager/worker
