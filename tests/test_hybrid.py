from hybrid_pool_executor import HybridPoolExecutor


def test_run():
    def simple_task():
        return "done"

    pool = HybridPoolExecutor()
    future = pool.submit(simple_task)
    assert future.result() == "done"
