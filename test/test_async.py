import asyncio
from hybrid_pool_executor import HybridPoolExecutor


async def func1():
    await asyncio.sleep(2)
    return 1


async def func2():
    await asyncio.sleep(2)
    return 2


async def async_main():
    executor = HybridPoolExecutor()
    future1 = executor.submit(func1)
    future2 = executor.submit(func2)
    return future1.get(), future2.get()


def test_run_in_async_pool():
    res = asyncio.run(async_main())
    res1, res2 = res
    assert res1 == 1
    assert res2 == 2
