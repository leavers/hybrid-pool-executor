|Build Status|
|PyPI|
|PyPI Version|

.. |Build Status| image:: https://github.com/leavers/hybrid-pool-executor/actions/workflows/tests.yml/badge.svg
   :target: https://github.com/leavers/hybrid-pool-executor/actions/workflows/tests.yml
.. |PyPI| image:: https://img.shields.io/pypi/v/hybrid-pool-executor.svg
   :target: https://pypi.org/project/hybrid-pool-executor/
.. |PyPI Version| image:: https://img.shields.io/pypi/pyversions/hybrid-pool-executor.svg
   :target: https://pypi.org/project/hybrid-pool-executor/

HybridPoolExecutor
==================

HybridPoolExecutor is a parallel executor that supports thead, process and async three operating models at the same time.

Example:

.. code-block:: python

    import asyncio
    import time
    import random

    from hybrid_pool_executor import HybridPoolExecutor


    def solve(v: int) -> int:
        print(f"You give worker a value {v}")
        time.sleep(random.randint(1, 4))
        print(f"The square of value {v} is {v * v}")
        return v


    async def solve_async(v: int) -> int:
        print(f"You give async worker a value {v}")
        await asyncio.sleep(random.randint(1, 4))
        print(f"The square of value {v} is {v * v}")
        return v


    async def main():
        pool = HybridPoolExecutor()

        # run in a thread
        future0 = pool.submit(solve, 0)

        # run in a process by setting "_mode" to "process"
        future1 = pool.submit(solve, 1, _mode="process")
        # or you can use a more precise approach: submit_task
        future2 = pool.submit_task(solve, kwargs={"v": 2}, mode="process")

        # run in an async worker
        future3 = pool.submit(solve_async, 3)  # run as a coroutine
        # you can also submit a coroutine
        future4 = pool.submit(solve_async(4))
        # async function/coroutine can be executed in thread/process too
        # run coroutine in a thread
        future5 = pool.submit(solve_async(5), _mode="thread")
        # or in a process
        future6 = pool.submit(solve_async, 6, _mode="process")

        # all futures can be get either synchronously or asynchronously
        await future0  # result from a thread worker
        await future1  # result from a process worker
        future2.result()  # result from a async worker
        # ......


    if __name__ == "__main__":
        asyncio.run(main())
