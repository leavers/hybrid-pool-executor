import asyncio
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


class AsyncToSync:
    def __init__(self, fn, /, *args, **kwargs):
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.is_coro = inspect.iscoroutine(self.fn)
        self.is_async = inspect.iscoroutinefunction(self.fn)

    def __call__(self, loop=None):
        if not self.is_coro and not self.is_async:
            return self.fn(*self.args, **self.kwargs)
        if self.is_async:
            self.fn = self.fn(*self.args, **self.kwargs)
        if not loop:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
        if loop.is_running():
            raise RuntimeError("Unable to execute when loop is already running.")
        return loop.run_until_complete(self.fn)


class KillableThread(Thread):
    @staticmethod
    def _raise_to_kill(tid, exctype):
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
        self._raise_to_kill(self.ident, exctype)

    def terminate(self):
        self.raise_exc(SystemExit)
