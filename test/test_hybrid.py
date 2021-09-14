import asyncio
import time
import random

from hybrid_pool_executor import HybridPoolExecutor


def func1():
    print('func1 starts')
    time.sleep(random.randint(1, 4))
    print('func1 ends')
    return 1


def func2():
    print('func2 starts')
    time.sleep(random.randint(1, 4))
    print('func2 ends')
    return 2


async def func3():
    print('func3 starts')
    await asyncio.sleep(random.randint(1, 4))
    print('func3 ends')
    return 3


def test_run():
    pool = HybridPoolExecutor()
    pool.submit(func1)  # run in a thread
    pool.submit(func2, mode='process')  # run in a process
    pool.submit(func3)  # run as a coroutine
