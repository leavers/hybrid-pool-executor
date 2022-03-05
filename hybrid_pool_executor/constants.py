from typing import Any, Callable, Coroutine, Union

Function = Union[Callable[..., Any], Coroutine[Any, Any, Any]]

PRESERVED_TASK_TAGS = frozenset({"async", "process", "thread"})

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
