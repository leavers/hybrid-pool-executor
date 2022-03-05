import asyncio
import dataclasses
import itertools
import typing as t
from dataclasses import dataclass, field
from functools import partial
from queue import Empty, SimpleQueue
from threading import Event, ThreadError
from time import monotonic

from hybrid_pool_executor.base import (
    Action,
    BaseManager,
    BaseManagerSpec,
    BaseTask,
    BaseWorker,
    BaseWorkerSpec,
    CancelledError,
    Function,
    Future,
    ModuleSpec,
)
from hybrid_pool_executor.constants import (
    ACT_CLOSE,
    ACT_DONE,
    ACT_EXCEPTION,
    ACT_NONE,
    ACT_RESET,
    ACT_RESTART,
)
from hybrid_pool_executor.utils import KillableThread, coalesce, isasync, rectify

NoneType = type(None)


@dataclass
class AsyncTask(BaseTask):
    future: Future = field(default_factory=Future)


@dataclass
class AsyncWorkerSpec(BaseWorkerSpec):
    max_task_count: int = -1
    max_err_count: int = 10
    daemon: bool = True


class AsyncWorker(BaseWorker):
    def __init__(self, spec: AsyncWorkerSpec):
        self._spec = dataclasses.replace(spec)
        self._name = self._spec.name

        self._loop: t.Optional[asyncio.AbstractEventLoop] = None
        self._async_tasks: t.Dict[str, asyncio.Task] = {}
        self._current_tasks: t.Dict[str, AsyncTask] = {}

        # bare bool vairiable may not be synced when using start()
        self._state: t.Dict[str, bool] = {
            "inited": False,
            "running": False,
            "idle": False,
        }
        self._thread: t.Optional[KillableThread] = None
        self._current_task_name: t.Optional[str] = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def spec(self) -> AsyncWorkerSpec:
        return self._spec

    def is_alive(self) -> bool:
        return self._state["running"]

    def start(self):
        if self._state["running"] or self._thread is not None:
            raise RuntimeError(
                f'{self.__class__.__qualname__} "{self._name}" is already started.'
            )
        self._thread = KillableThread(target=self._run, daemon=self._spec.daemon)
        self._thread.start()

        # Block method until self._run actually starts to avoid creating multiple
        # workers when in high concurrency situation.
        state = self._state
        while not state["inited"]:
            pass

    async def _async_run(self):
        state = self._state
        state["running"] = True
        state["idle"] = True
        state["inited"] = True

        spec = self._spec
        worker_name: str = self._name
        task_bus: SimpleQueue = spec.task_bus
        request_bus: SimpleQueue = spec.request_bus
        response_bus: SimpleQueue = spec.response_bus
        max_task_count: int = spec.max_task_count
        max_err_count: int = spec.max_err_count
        max_cons_err_count: int = spec.max_cons_err_count
        idle_timeout: float = spec.idle_timeout
        wait_interval: float = spec.wait_interval

        loop = t.cast(asyncio.BaseEventLoop, self._loop)
        async_tasks = self._async_tasks
        async_response_bus = asyncio.Queue()

        async def coroutine(
            task: AsyncTask,
            worker_name: str,
            bus: asyncio.Queue,
        ):
            result = resp = None
            try:
                task.fn = t.cast(t.Callable[..., t.Any], task.fn)
                result = await task.fn(*task.args, **task.kwargs)
            except Exception as exc:
                resp = Action(
                    flag=ACT_EXCEPTION,
                    task_name=task.name,
                    worker_name=worker_name,
                )
                task.future.set_exception(exc)
            else:
                resp = Action(
                    flag=ACT_DONE,
                    task_name=task.name,
                    worker_name=worker_name,
                )
                task.future.set_result(result)
            finally:
                await bus.put(resp)

        task_count: int = 0
        err_count: int = 0
        cons_err_count: int = 0
        current_coroutines: int = 0
        is_prev_coro_err: bool = False

        response = None
        idle_tick = monotonic()
        while True:
            if current_coroutines > 0:
                state["idle"] = False
                idle_tick = monotonic()
            else:
                state["idle"] = True
                if monotonic() - idle_tick > idle_timeout:
                    response = Action(flag=ACT_CLOSE, worker_name=worker_name)
                    break
            while not request_bus.empty():
                request: Action = request_bus.get()
                if request.match(ACT_RESET):
                    task_count = 0
                    err_count = 0
                    cons_err_count = 0
                if request.match(ACT_CLOSE, ACT_RESTART):
                    response = Action(flag=request.flag, worker_name=worker_name)
                    break
            if not state["running"]:
                break
            try:
                while not 0 <= max_task_count <= task_count:
                    task: AsyncTask = task_bus.get(timeout=wait_interval)
                    # check if future is cancelled
                    if task.cancelled:
                        task_bus.put(
                            Action(
                                flag=ACT_EXCEPTION,
                                task_name=task.name,
                                worker_name=worker_name,
                                exception=CancelledError(
                                    f'Future "{task.name}" has been cancelled'
                                ),
                            )
                        )
                        del task
                        continue
                    else:
                        state["idle"] = False
                        async_task: asyncio.Task = loop.create_task(
                            coroutine(
                                task=task,
                                worker_name=worker_name,
                                bus=async_response_bus,
                            )
                        )
                        async_tasks[task.name] = async_task
                        del task
                    task_count += 1
                    current_coroutines += 1
            except Empty:
                pass
            await asyncio.sleep(0)  # ugly but works
            while not async_response_bus.empty():
                response = await async_response_bus.get()
                await async_tasks.pop(response.task_name)
                current_coroutines -= 1
                if response.match(ACT_EXCEPTION):
                    err_count += 1
                    if is_prev_coro_err:
                        cons_err_count += 1
                    else:
                        cons_err_count = 1
                    is_prev_coro_err = True
                else:
                    cons_err_count = 0
                    is_prev_coro_err = False
                if (
                    0 <= max_task_count <= task_count
                    or 0 <= max_err_count <= err_count
                    or 0 <= max_cons_err_count <= cons_err_count
                ):
                    response.add_flag(ACT_RESTART)
                    response_bus.put(response)
                    state["running"] = False
                    break
                response_bus.put(response)
                response = None
            if not state["running"]:
                break
        state["running"] = False
        for async_task in async_tasks.values():
            if not async_task.done():
                await async_task
        if response is not None and response.flag != ACT_NONE:
            if not response.match(ACT_CLOSE, ACT_RESTART):
                response_bus.put(response)
            else:
                await async_response_bus.put(response)
        while not async_response_bus.empty():
            response = await async_response_bus.get()
            response_bus.put(response)

    def _run(self):
        self._loop = asyncio.new_event_loop()
        self._loop.run_until_complete(self._async_run())

    def stop(self):
        self._state["running"] = False
        if self._thread and self._thread.is_alive():
            self._thread.join()
        self._thread = None

    def terminate(self):
        self._state["running"] = False
        try:
            if self._thread and self._thread.is_alive():
                self._thread.terminate()
        except ThreadError:
            pass
        self._thread = None

    def is_idle(self) -> bool:
        state = self._state
        return state["idle"] if state["running"] else False


@dataclass
class AsyncManagerSpec(BaseManagerSpec):
    mode: str = "async"
    num_workers: int = 1
    name_pattern: str = "AsyncManager-{manager_seq}"
    # -1: unlimited; 0: same as num_workers
    max_processing_responses_per_iteration: int = -1
    worker_name_pattern: str = "AsyncWorker-{worker} [{manager}]"
    task_name_pattern: str = "AsyncTask-{task} [{manager}]"
    default_worker_spec: AsyncWorkerSpec = field(
        default_factory=partial(AsyncWorkerSpec, name="DefaultWorkerSpec")
    )


class AsyncManager(BaseManager):
    _next_manager_seq = itertools.count().__next__

    def __init__(self, spec: AsyncManagerSpec):
        self._spec = dataclasses.replace(spec)
        self._default_worker_spec = dataclasses.replace(spec.default_worker_spec)
        self._name = self._spec.name_pattern.format(
            manager_seq=self._next_manager_seq()
        )

        self._next_worker_seq = itertools.count().__next__
        self._next_task_seq = itertools.count().__next__

        self._state: t.Dict[str, bool] = {
            "inited": False,
            "running": False,
        }

        self._task_bus = SimpleQueue()
        self._response_bus = SimpleQueue()
        self._current_workers: t.Dict[str, AsyncWorker] = {}
        self._current_tasks: t.Dict[str, t.Any] = {}
        self._thread: t.Optional[KillableThread] = None

    def start(self):
        if self._state["running"] or self._thread is not None:
            raise RuntimeError(f'ThreadManager "{self._name}" is already started.')
        self._thread = KillableThread(target=self._run, daemon=True)
        self._thread.start()

        state = self._state
        while not state["inited"]:
            pass

    def is_alive(self) -> bool:
        return self._state["running"]

    def _run(self):
        state = self._state
        state["running"] = True
        state["inited"] = True

        metronome = Event()
        wait_interval: float = rectify(coalesce(self._spec.wait_interval, 0.1), 0.1)
        num_process_limit: int = rectify(
            coalesce(self._spec.max_processing_responses_per_iteration, -1), -1
        )
        response_bus = self._response_bus
        while True:
            if not state["running"]:
                break

            num_processed: int = 0
            while not response_bus.empty():
                self._consume_response()
                num_processed += 1
                if num_processed >= num_process_limit:
                    break
            if num_processed == 0:
                metronome.wait(wait_interval)
        while not response_bus.empty():
            self._consume_response()
        self._stop_all_workers()

    def _stop_all_workers(self):
        stop_action = Action(ACT_CLOSE)
        for worker in self._current_workers.values():
            worker.spec.request_bus.put(stop_action)
        for worker in self._current_workers.values():
            worker.stop()

    def stop(self, timeout: float = 5.0):
        self._state["running"] = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=rectify(coalesce(timeout, 5.0), 5.0))
        self._thread = None

    def terminate(self):
        self._state["running"] = False
        try:
            if self._thread and self._thread.is_alive():
                self._thread.terminate()
        except ThreadError:
            pass
        self._thread = None

    def get_worker_spec(
        self,
        name: t.Optional[str] = None,
        daemon: t.Optional[bool] = None,
        idle_timeout: t.Optional[float] = None,
        wait_interval: t.Optional[float] = None,
        max_task_count: t.Optional[int] = None,
        max_err_count: t.Optional[int] = None,
        max_cons_err_count: t.Optional[int] = None,
    ) -> AsyncWorkerSpec:
        if name and name in self._current_tasks:
            raise KeyError(f'Worker "{name}" exists.')
        worker_spec = AsyncWorkerSpec(
            name=coalesce(
                name,
                self._spec.worker_name_pattern.format(
                    manager=self._name,
                    worker=self._next_worker_seq(),
                ),
            ),
            task_bus=self._task_bus,
            request_bus=SimpleQueue(),
            response_bus=self._response_bus,
            daemon=coalesce(daemon, self._default_worker_spec.daemon),
            idle_timeout=rectify(
                coalesce(idle_timeout, self._default_worker_spec.idle_timeout),
                self._default_worker_spec.idle_timeout,
            ),
            wait_interval=rectify(
                coalesce(wait_interval, self._default_worker_spec.wait_interval),
                self._default_worker_spec.wait_interval,
            ),
            max_task_count=rectify(
                coalesce(max_task_count, self._default_worker_spec.max_task_count),
                self._default_worker_spec.max_task_count,
            ),
            max_err_count=rectify(
                coalesce(max_err_count, self._default_worker_spec.max_err_count),
                self._default_worker_spec.max_err_count,
            ),
            max_cons_err_count=rectify(
                coalesce(
                    max_cons_err_count, self._default_worker_spec.max_cons_err_count
                ),
                self._default_worker_spec.max_cons_err_count,
            ),
        )
        return worker_spec

    def _get_task_name(self, name: t.Optional[str] = None) -> str:
        if name:
            if name in self._current_tasks:
                raise KeyError(f'Task "{name}" exists.')
            return name
        return coalesce(
            name,
            self._spec.task_name_pattern.format(
                manager=self._name,
                task=self._next_task_seq(),
            ),
        )

    def submit(
        self,
        fn: Function,
        args: t.Optional[t.Iterable[t.Any]] = (),
        kwargs: t.Optional[t.Dict[str, t.Any]] = None,
        name: t.Optional[str] = None,
    ) -> Future:
        if not self._state["running"]:
            raise RuntimeError(
                f'Manager "{self._name}" is either stopped or not started yet '
                "and not able to accept tasks."
            )
        if not isasync(fn):
            raise NotImplementedError(
                f'Param "fn" ({fn}) is neither a coroutine nor a coroutine function.'
            )
        name = self._get_task_name(name)
        future = Future()
        task = AsyncTask(
            name=name,
            fn=fn,
            args=args or (),
            kwargs=kwargs or {},
            future=future,
        )
        self._current_tasks[name] = task
        self._task_bus.put(task)
        self._adjust_workers()
        return future

    def _consume_response(self):
        response: Action = self._response_bus.get()
        response.task_name = t.cast(str, response.task_name)
        response.worker_name = t.cast(str, response.worker_name)
        if response.match(ACT_DONE, ACT_EXCEPTION):
            self._current_tasks.pop(response.task_name)
        if response.match(ACT_CLOSE):
            self._current_workers.pop(response.worker_name)
        elif response.match(ACT_RESTART):
            worker = self._current_workers[response.worker_name]
            worker.stop()
            worker.start()

    def _adjust_iterator(self) -> range:
        if self._spec.incremental or self._spec.num_workers < 0:
            qsize = self._task_bus.qsize()
            num_idle_workers: int = sum(
                1 if w.is_idle() else 0 for w in self._current_workers.values()
            )
            if self._spec.num_workers < 0:
                iterator = range(qsize - num_idle_workers)
            else:
                num_curr_workers: int = len(self._current_workers)
                iterator = range(
                    num_curr_workers,
                    min(
                        self._spec.num_workers,
                        num_curr_workers + qsize - num_idle_workers,
                    ),
                )
        else:
            iterator = range(len(self._current_workers), self._spec.num_workers)
        return iterator

    def _adjust_workers(self):
        # return if the number of workers already meets requirements
        # works on both incremental and static mode
        if len(self._current_workers) == self._spec.num_workers:
            return
        # if more workers are needed, create them
        for _ in self._adjust_iterator():
            worker = AsyncWorker(self.get_worker_spec())
            self._current_workers[worker._name] = worker
            worker.start()


MODULE_SPEC = ModuleSpec(
    name="async",
    manager_class=AsyncManager,
    manager_spec_class=AsyncManagerSpec,
    worker_class=AsyncWorker,
    worker_spec_class=AsyncWorkerSpec,
    tags=frozenset({"async"}),
    enabled=True,
)
