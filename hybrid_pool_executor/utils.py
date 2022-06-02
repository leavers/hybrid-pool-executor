import asyncio
import ctypes
import functools
import inspect
import typing as t
import weakref
from operator import le
from threading import Lock, Thread
from types import MethodType

T = t.TypeVar("T")


def coalesce(*args) -> t.Any:
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
    operator: t.Callable = le,
) -> int:
    return fallback if operator(val, threshold) else val


@rectify.register(float)
def _(
    val: float,
    fallback: float = -1.0,
    threshold: float = 0.0,
    operator: t.Callable = le,
) -> float:
    return fallback if operator(val, threshold) else val


@rectify.register(type(None))  # type: ignore
def _(*args, **kwargs):
    raise TypeError('Param "val" should not be None.')


iscoroutine = inspect.iscoroutine
iscoroutinefunction = inspect.iscoroutinefunction
ismethod = inspect.ismethod


def isasync(object: t.Any):
    return iscoroutine(object) or iscoroutinefunction(object)


_singleton_instances = {}
_singleton_lock = Lock()


class Singleton:
    def __new__(cls, *args, **kwargs):
        if cls not in _singleton_instances:
            with _singleton_lock:
                if cls not in _singleton_instances:
                    _singleton_instances[cls] = object.__new__(cls, *args, **kwargs)
        return _singleton_instances[cls]


class SingletonMeta(type):
    def __call__(cls, *args, **kwargs):
        if cls not in _singleton_instances:
            with _singleton_lock:
                if cls not in _singleton_instances:
                    _singleton_instances[cls] = super().__call__(*args, **kwargs)
        return _singleton_instances[cls]


def get_event_loop(
    loop: t.Optional[asyncio.AbstractEventLoop] = None,
) -> asyncio.AbstractEventLoop:
    if loop:
        return loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop


class AsyncToSync:
    def __init__(self, fn, /, *args, **kwargs):
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.is_coro = iscoroutine(self.fn)
        self.is_func = iscoroutinefunction(self.fn)

    def __call__(self, loop=None):
        if not self.is_coro and not self.is_func:
            return self.fn(*self.args, **self.kwargs)
        if self.is_func:
            self.fn = self.fn(*self.args, **self.kwargs)
        loop = get_event_loop(loop)
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


class WeakClassMethod:
    def __init__(self, method: MethodType) -> None:
        if not ismethod(method):
            raise TypeError(f"Object {method} is not a class method")
        self._cls_ref = weakref.ref(method.__self__)
        self._name = method.__name__

    def __call__(self, *args, **kwargs) -> t.Any:
        return getattr(self._cls_ref(), self._name)(*args, **kwargs)
