import ctypes
import functools
import inspect
from operator import le
from threading import Thread
from typing import Any, Callable, TypeVar

NoneType = type(None)
T = TypeVar("T")


def coalesce(*args) -> Any:
    for arg in args:
        if arg is not None:
            return arg
    return None


@functools.singledispatch
def rectify(val, fallback=None, threshold=None, operator=le):
    return fallback if operator(val, threshold) else val


@rectify.register(int)
def _(
    val: int,
    fallback: int = -1,
    threshold: int = 0,
    operator: Callable = le,
) -> int:
    return fallback if operator(val, threshold) else val


@rectify.register(float)
def _(
    val: float,
    fallback: float = -1.0,
    threshold: float = 0.0,
    operator: Callable = le,
) -> float:
    return fallback if operator(val, threshold) else val


@rectify.register(NoneType)
def _(*args, **kwargs):
    raise TypeError('Param "val" should not be None.')


class KillableThread(Thread):
    @staticmethod
    def _raise_to_kill(tid, exctype):
        """Raises the exception, performs cleanup if needed."""
        if not inspect.isclass(exctype):
            raise TypeError("Only types can be raised (not instances)")
        res = ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, ctypes.py_object(exctype))
        if res == 0:
            raise ValueError(f'Invalid thread id "{tid}"')
        elif res != 1:
            # if it returns a number greater than one, you're in trouble,
            # and you should call it again with exc=NULL to revert the effect
            ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, 0)
            raise SystemError("PyThreadState_SetAsyncExc failed.")

    def raise_exc(self, exctype):
        """raises the given exception type in the context of this thread"""
        self._raise_to_kill(self.ident, exctype)

    def terminate(self):
        """raises SystemExit in the context of the given thread, which should
        cause the thread to exit silently (unless caught)"""
        self.raise_exc(SystemExit)
