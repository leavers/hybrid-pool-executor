import contextvars

import pytest

from hybrid_pool_executor.utils import AsyncToSync


@pytest.mark.timeout(10)
def test_async_to_sync():
    async def func1(v):
        return v + 1

    async def func2(v):
        return await func1(v) + 2

    def sync_func(v):
        return v + 1

    assert AsyncToSync(func1)(1) == 2
    assert AsyncToSync(func2)(1) == 4
    assert AsyncToSync(func1(1))() == 2
    assert AsyncToSync(func2(1))() == 4
    assert AsyncToSync(sync_func)(1) == 2

    f = AsyncToSync(func2)
    assert f(1) == 4
    assert f(2) == 5


@pytest.mark.timeout(10)
def test_async_to_sync_contextvars():
    cvar = contextvars.ContextVar("var")
    cvar.set("a")

    async def func1():
        return cvar.get()

    async def func2():
        func1_var = await func1()
        cvar.set("c")
        return func1_var + cvar.get()

    f = AsyncToSync(func1)
    assert f() == "a"
    cvar.set("b")
    assert f() == "b"
    assert AsyncToSync(func2)() == "bc"
