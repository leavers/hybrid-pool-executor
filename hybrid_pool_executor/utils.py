import asyncio
import contextvars
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
    def __init__(self, fn, /, loop: t.Optional[asyncio.AbstractEventLoop] = None):
        self.fn = fn
        self.loop = loop

    @staticmethod
    def _restore_context(context):
        for cvar in context:
            new_var = context.get(cvar)
            try:
                if cvar.get() != new_var:
                    cvar.set(new_var)
            except LookupError:
                cvar.set(new_var)

    @staticmethod
    async def _wrap(coro, context_list):
        AsyncToSync._restore_context(context_list[0])
        return await coro

    def __call__(self, *args, **kwargs):
        is_coro = iscoroutine(self.fn)
        is_coro_func = iscoroutinefunction(self.fn)

        if not is_coro and not is_coro_func:
            return self.fn(*args, **kwargs)
        if is_coro:
            if args or kwargs:
                raise RuntimeError(
                    "Coroutine should not be invoked with args or kwargs."
                )
            coro = self.fn
        else:
            coro = self.fn(*args, **kwargs)

        loop = get_event_loop(self.loop)
        if loop.is_running():
            raise RuntimeError("Unable to execute when loop is already running.")
        context_list = [contextvars.copy_context()]
        try:
            result = loop.run_until_complete(AsyncToSync._wrap(coro, context_list))
        finally:
            AsyncToSync._restore_context(context_list[0])
        return result


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
