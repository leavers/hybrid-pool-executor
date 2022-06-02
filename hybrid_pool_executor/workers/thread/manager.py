import dataclasses
import itertools
import os
import typing as t
from dataclasses import dataclass, field
from functools import partial
from threading import Event, ThreadError
from time import monotonic

from hybrid_pool_executor.base import (
    Action,
    BaseManager,
    BaseManagerSpec,
    Future,
    adjust_worker_iterator,
)
from hybrid_pool_executor.constants import (
    ACT_CLOSE,
    ACT_DONE,
    ACT_EXCEPTION,
    ACT_RESTART,
    Function,
)
from hybrid_pool_executor.utils import (
    KillableThread,
    WeakClassMethod,
    coalesce,
    rectify,
)
from hybrid_pool_executor.workers.thread.worker import (
    ThreadTask,
    ThreadWorker,
    ThreadWorkerSpec,
)


@dataclass
class ThreadManagerSpec(BaseManagerSpec):
    name_pattern: str = "ThreadManager-{manager_seq}"
    worker_name_pattern: str = "ThreadWorker-{worker_seq} [{manager}]"
    task_name_pattern: str = "ThreadTask-{task_seq} [{manager}]"
    # -1: unlimited; 0: same as num_workers
    max_processing_responses_per_iteration: int = -1
    task_class: t.Type[ThreadTask] = ThreadTask
    worker_class: t.Type[ThreadWorker] = ThreadWorker
    worker_spec: ThreadWorkerSpec = field(
        default_factory=partial(ThreadWorkerSpec, name="DefaultWorkerSpec")
    )

    def __post_init__(self):
        if self.num_workers == 0:
            self.num_workers == os.cpu_count() * 2
        if self.max_processing_responses_per_iteration == 0:
            self.max_processing_responses_per_iteration = self.num_workers


class ThreadManager(BaseManager):
    _next_manager_seq = itertools.count().__next__

    def __init__(self, spec: ThreadManagerSpec):
        super().__init__()
        self._spec = dataclasses.replace(spec)
        self._worker_class = self._spec.worker_class
        self._worker_spec = dataclasses.replace(spec.worker_spec)
        self._name = self._spec.name_pattern.format(
            manager_seq=self.__class__._next_manager_seq()
        )

        self._next_worker_seq = itertools.count().__next__
        self._next_task_seq = itertools.count().__next__

        self._task_bus = self._worker_spec.task_bus_type()
        self._response_bus = self._worker_spec.request_bus_type()
        self._curr_workers: t.Dict[str, ThreadWorker] = {}
        self._curr_tasks: t.Dict[str, t.Any] = {}
        self._thread: t.Optional[KillableThread] = None

    def start(self):
        if self._state.running or self._thread is not None:
            raise RuntimeError(
                f'{self.__class__.__name__} "{self._name}" is already started.'
            )
        self._thread = KillableThread(
            target=WeakClassMethod(self._run),
            daemon=True,
        )
        self._thread.start()

        state = self._state
        while not state.inited:
            pass

    def _run(self):
        state = self._state
        state.running = True
        state.inited = True

        metronome = Event()
        wait_interval: float = rectify(coalesce(self._spec.wait_interval, 0.1), 0.1)
        idle_timeout: float = rectify(coalesce(self._spec.idle_timeout, 60), 60)
        num_process_limit: int = rectify(
            coalesce(self._spec.max_processing_responses_per_iteration, -1), -1
        )
        curr_tasks = self._curr_tasks
        consume_response = self._consume_response
        response_bus = self._response_bus

        idle_tick = monotonic()
        while True:
            if not curr_tasks and response_bus.empty():
                if idle_timeout >= 0 and monotonic() - idle_tick > idle_timeout:
                    break
            else:
                idle_tick = monotonic()
            if not state.running:
                break

            num_processed: int = 0
            while not response_bus.empty():
                consume_response()
                num_processed += 1
                if num_processed >= num_process_limit:
                    break
            if num_processed == 0:
                metronome.wait(wait_interval)

        state.running = False
        while not response_bus.empty():
            consume_response()
        self._stop_all_workers()
        self._thread = None

    def _stop_all_workers(self):
        stop_action = Action(ACT_CLOSE)
        for worker in self._curr_workers.values():
            worker.spec.request_bus.put(stop_action)
        for worker in self._curr_workers.values():
            worker.stop()

    def stop(self, timeout: t.Optional[float] = 60.0):
        self._state.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread and self._thread.is_alive():
                raise RuntimeError(f"Failed to stop {self.__class__.__name__}")
        self._thread = None

    def terminate(self):
        self._state.running = False
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
    ) -> ThreadWorkerSpec:
        if name and name in self._curr_tasks:
            raise KeyError(f'Worker "{name}" exists.')
        default_spec = self._worker_spec
        return default_spec.__class__(
            name=coalesce(
                name,
                self._spec.worker_name_pattern.format(
                    manager=self._name,
                    worker_seq=self._next_worker_seq(),
                ),
            ),
            task_bus=self._task_bus,
            request_bus=default_spec.request_bus_type(),
            response_bus=self._response_bus,
            daemon=coalesce(daemon, default_spec.daemon),
            idle_timeout=rectify(
                coalesce(idle_timeout, default_spec.idle_timeout),
                default_spec.idle_timeout,
            ),
            wait_interval=rectify(
                coalesce(wait_interval, default_spec.wait_interval),
                default_spec.wait_interval,
            ),
            max_task_count=rectify(
                coalesce(max_task_count, default_spec.max_task_count),
                default_spec.max_task_count,
            ),
            max_err_count=rectify(
                coalesce(max_err_count, default_spec.max_err_count),
                default_spec.max_err_count,
            ),
            max_cons_err_count=rectify(
                coalesce(max_cons_err_count, default_spec.max_cons_err_count),
                default_spec.max_cons_err_count,
            ),
        )

    def _get_task_name(self, name: t.Optional[str] = None) -> str:
        if name:
            if name in self._curr_tasks:
                raise KeyError(f'Task "{name}" exists.')
            return name
        return coalesce(
            name,
            self._spec.task_name_pattern.format(
                manager=self._name,
                task_seq=self._next_task_seq(),
            ),
        )

    def submit(
        self,
        fn: Function,
        args: t.Optional[t.Iterable[t.Any]] = (),
        kwargs: t.Optional[t.Dict[str, t.Any]] = None,
        name: t.Optional[str] = None,
    ) -> Future:
        self._ensure_running()
        name = self._get_task_name(name)
        future = Future()
        task = self._spec.task_class(
            name=name,
            fn=fn,
            args=args or (),
            kwargs=kwargs or {},
            future=future,
        )
        self._curr_tasks[name] = task
        self._task_bus.put(task)
        self._adjust_workers()
        return future

    def _consume_response(self):
        response: Action = self._response_bus.get()
        response.task_name = t.cast(str, response.task_name)
        response.worker_name = t.cast(str, response.worker_name)
        if response.match(ACT_DONE, ACT_EXCEPTION):
            self._curr_tasks.pop(response.task_name)
        if response.match(ACT_CLOSE):
            self._curr_workers.pop(response.worker_name)
        elif response.match(ACT_RESTART):
            worker = self._curr_workers[response.worker_name]
            worker.stop()
            worker.start()

    def _adjust_workers(self):
        # return if the number of workers already meets requirements
        # works on both incremental and static mode
        if len(self._curr_workers) == self._spec.num_workers:
            return
        # if more workers are needed, create them
        for _ in adjust_worker_iterator(
            spec=self._spec,
            curr_workers=self._curr_workers.values(),
            num_curr_tasks=self._task_bus.qsize(),
        ):
            worker = self._worker_class(self.get_worker_spec())
            self._curr_workers[worker._name] = worker
            worker.start()
