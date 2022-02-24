import functools
from operator import le
from types import NoneType
from typing import Any, Callable, Optional, TypeVar

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
