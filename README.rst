|Build Status|

HybridPoolExecutor
==================

HybridPoolExecutor is a parallel executor that supports thead, process and async three operating models at the same time.

Example:

.. code-block:: python

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
        future1 = pool.submit(func1)  # run in a thread
        future2 = pool.submit(func2, _mode='process')  # run in a process
        future3 = pool.submit(func3)  # run as a coroutine


    if __name__ == '__main__':
        test_run()

