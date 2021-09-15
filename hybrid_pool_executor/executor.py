import asyncio
import atexit
import itertools
import multiprocessing as mp
import os
import socket
import threading
import time
import warnings
import weakref
from dataclasses import dataclass, field
from queue import SimpleQueue, Empty
from typing import Any, Callable, Dict, Hashable, Literal, Optional, Tuple, Union
from weakref import ReferenceType

MAX_THREADS = 64
MAX_PROCESSES = 32

WORK_MODE_THREAD = 'thread'
WORK_MODE_PROCESS = 'process'
WORK_MODE_ASYNC = 'async'
WORK_MODE_LOCAL = 'local'
work_modes = (WORK_MODE_THREAD, WORK_MODE_PROCESS, WORK_MODE_ASYNC, WORK_MODE_LOCAL)
WorkMode = Literal['thread', 'process', 'async', 'local']
ThreadFallbackMode = Literal['process', 'local']
ProcessFallbackMode = Literal['thread', 'local']
AsyncFallbackMode = Literal['thread', 'local']

WORKFLOW_MODE_MIX = 'mix'
WORKFLOW_MODE_BFS = 'bfs'
WORKFLOW_MODE_DFS = 'dfs'
workflow_modes = (WORKFLOW_MODE_MIX, WORKFLOW_MODE_BFS, WORKFLOW_MODE_DFS)
WorkflowMode = Literal['mix', 'bfs', 'dfs']

ERROR_MODE_RAISE = 'raise'
ERROR_MODE_IGNORE = 'ignore'
ERROR_MODE_COERCE = 'coerce'
error_modes = (ERROR_MODE_RAISE, ERROR_MODE_IGNORE, ERROR_MODE_COERCE)
ErrorMode = Literal['raise', 'ignore', 'coerce']

ACT_NONE = 0
ACT_DONE = 1
ACT_CLOSE = 1 << 1
ACT_EXCEPTION = 1 << 2
ACT_RESTART = 1 << 3
ACT_RESET = 1 << 4
ACT_TIMEOUT = 1 << 5
ACT_CANCEL = 1 << 6
ACT_COERCE = 1 << 7

ACT_CMD_WORKER_STOP_FLAGS = (ACT_CLOSE, ACT_RESTART)  # used for manager/worker


class CancelledError(Exception):
    pass


_manager_threads = weakref.WeakSet()
_global_shutdown = False


# TODO: Wrap executor class to make workflow be able to use other executor implementations.
# TODO: Add KeyboardInterrupt, SystemExit handler (optional).

@atexit.register
def _python_exit():
    global _global_shutdown
    _global_shutdown = True
    for t in _manager_threads:
        t.join()


@dataclass
class _ActionItem:
    action: int = ACT_NONE
    work_id: Hashable = None
    worker_id: Hashable = None
    result: Any = None
    exception: BaseException = None
    settings: Any = None

    def add_action(self, action: int):
        self.action |= action

    def match(self, *actions) -> bool:
        if len(actions) == 1 and isinstance(actions[0], (tuple, list, set)):
            actions = actions[0]
        for action in actions:
            if self.action & action:
                return True
        return False


@dataclass
class _CallItem:
    name: Hashable
    func: Callable[..., Any]
    args: Tuple[Any, ...] = ()
    kwargs: Dict[str, Any] = field(default_factory=dict)
    work_mode: WorkMode = WORK_MODE_THREAD
    cancelled: bool = False


class PollFuture:
    def __init__(self, name: Hashable, call_item: _CallItem = None):
        self._name = name
        self._result: Any = None
        self._cancelled: Optional[bool] = None
        self._exc: Optional[BaseException] = None
        self._got: threading.Event = threading.Event()
        self._put_s, self._get_s = socket.socketpair()
        self._call_item_ref = weakref.ref(call_item)
        # prevent "loop not found" RuntimeError when running in pytest
        try:
            self._loop = asyncio.get_event_loop_policy().get_event_loop()
        except RuntimeError:
            self._loop = asyncio.get_event_loop_policy().new_event_loop()
            asyncio.set_event_loop(self._loop)
        self._async_got: asyncio.Event = asyncio.Event()

    @property
    def name(self):
        return self._name

    @property
    def sentinel(self):
        return self._get_s.fileno()

    @property
    def result(self):
        return self.get()

    @property
    def exception(self):
        return self._exc

    @property
    def cancelled(self):
        return self._cancelled

    def cancel(self):
        if self._cancelled is not None:
            return self._cancelled
        if not self.ready():
            call_item: _CallItem = self._call_item_ref()
            if call_item:
                call_item.cancelled = True
            self._cancelled = True
        else:
            self._cancelled = False
        return self._cancelled

    def ready(self) -> bool:
        return not self._cancelled and self._got.is_set()

    def get(self):
        if self._cancelled:
            raise CancelledError(f'Future "{self._name}" has been cancelled')
        if not self._got.is_set():
            self._got.wait()
            self._get_s.recv(1)
        if self._exc:
            raise self._exc
        return self._result

    async def get_async(self):
        await self._async_got.wait()
        return self.get()

    def set_result(
            self,
            result: Any = None,
            exception: BaseException = None
    ):
        if self._got.is_set():
            raise RuntimeError(f'Future result can only be set once.')
        self._result = result
        self._exc = exception
        self._put_s.send(b'1')
        self.notify_async_event()
        self._got.set()

    def notify_async_event(self):

        # As run_coroutine_threadsafe only accepts coroutine, we need to wrap event.set()
        # method into an async function.
        async def set_async(event):
            event.set()

        asyncio.run_coroutine_threadsafe(set_async(self._async_got), self._loop)


@dataclass
class _WorkItem:
    name: Hashable
    future: PollFuture
    call_item: _CallItem


@dataclass
class _AsyncWorkerContext:
    name: Hashable
    work_queue: SimpleQueue  # SimpleQueue of _CallItem
    request_queue: SimpleQueue  # SimpleQueue of _ActionItem
    response_queue: SimpleQueue  # SimpleQueue of _ActionItem
    daemon: bool = True
    idle_timeout: float = 60.0
    wait_interval: float = 0.1
    max_work_count: int = 12
    max_err_count: int = 3


@dataclass
class _ThreadWorkerContext:
    name: Hashable
    work_queue: SimpleQueue  # SimpleQueue of _CallItem
    request_queue: SimpleQueue  # SimpleQueue of _ActionItem
    response_queue: SimpleQueue  # SimpleQueue of _ActionItem
    daemon: bool = True
    idle_timeout: float = 60.0
    wait_interval: float = 0.1
    max_work_count: int = 12
    max_err_count: int = 3
    max_cons_err_count: int = -1


@dataclass
class _ProcessWorkerContext:
    name: Hashable
    work_queue: mp.Queue  # Queue of _CallItem
    request_queue: mp.Queue  # Queue of _ActionItem
    response_queue: mp.Queue  # Queue of _ActionItem
    process_context: mp.context.BaseContext
    daemon: bool = False
    idle_timeout: float = 60.0
    wait_interval: float = 0.1
    max_work_count: int = 1
    max_err_count: int = -1
    max_cons_err_count: int = -1


class _AsyncWorker:

    def __init__(
            self,
            ctx: _AsyncWorkerContext = None
    ):
        self.ctx = ctx
        self._name = ctx.name
        self._daemon = ctx.daemon

        self._async_loop: Optional[asyncio.BaseEventLoop] = None
        self._async_tasks: Dict[Hashable, asyncio.Task] = {}

        self._running: bool = False
        self._exit: bool = True
        self._thread: Optional[threading.Thread] = None

    @classmethod
    async def _coroutine(
            cls,
            async_func: Callable[..., Any],
            args: Tuple[Any, ...],
            kwargs: Dict[str, Any],
            work_id: Hashable,
            worker_id: Hashable,
            async_out: asyncio.Queue
    ):
        result = response = None
        try:
            result = await async_func(*args, **kwargs)
        except Exception as exc:
            response = _ActionItem(action=ACT_EXCEPTION,
                                   work_id=work_id,
                                   worker_id=worker_id,
                                   result=result,
                                   exception=exc)
        else:
            response = _ActionItem(action=ACT_DONE,
                                   work_id=work_id,
                                   worker_id=worker_id,
                                   result=result)
        finally:
            await async_out.put(response)

    async def _manager_coroutine(self):
        self._exit = False
        ctx = self.ctx
        worker_id = ctx.name
        wait_interval = ctx.wait_interval
        idle_timeout = ctx.idle_timeout

        async_response_queue = asyncio.Queue()

        async_tasks = self._async_tasks
        async_start = asyncio.Event()
        async_start.set()
        coroutine = self._coroutine
        work_queue = ctx.work_queue
        request_queue = ctx.request_queue
        response_queue = ctx.response_queue

        work_count = 0
        err_count = 0
        curr_coroutines = 0

        response: Optional[_ActionItem] = None
        self._running = True
        idle_tick = time.monotonic()

        while not self._exit:
            if curr_coroutines > 0:
                idle_tick = time.monotonic()
            else:
                if time.monotonic() - idle_tick > idle_timeout:
                    response = _ActionItem(ACT_CLOSE | ACT_TIMEOUT, worker_id=worker_id)
                    break
            while not request_queue.empty():
                request: _ActionItem = request_queue.get()
                if request.match(ACT_RESET):
                    work_count = 0
                    err_count = 0
                if request.match(ACT_CMD_WORKER_STOP_FLAGS):
                    response = _ActionItem(request.action, worker_id=worker_id)
                    self._exit = True
                    break
            if self._exit:
                break
            try:
                while not 0 <= ctx.max_work_count <= work_count:
                    call_item: _CallItem = work_queue.get(timeout=wait_interval)
                    work_id = call_item.name
                    # Check if future is cancelled
                    if call_item.cancelled:
                        response_queue.put(
                            _ActionItem(action=ACT_EXCEPTION,
                                        work_id=work_id,
                                        worker_id=worker_id,
                                        exception=CancelledError(f'Future "{work_id}" has been cancelled'))
                        )
                        continue
                    else:
                        task: asyncio.Task = asyncio.create_task(
                            coroutine(async_func=call_item.func,
                                      args=call_item.args,
                                      kwargs=call_item.kwargs,
                                      work_id=work_id,
                                      worker_id=worker_id,
                                      async_out=async_response_queue)
                        )
                    async_tasks[work_id] = task
                    work_count += 1
                    curr_coroutines += 1
            except Empty:
                pass
            await asyncio.sleep(0)  # ugly but works
            while not async_response_queue.empty():
                response: _ActionItem = await async_response_queue.get()
                await async_tasks.pop(response.work_id)
                curr_coroutines -= 1
                if response.match(ACT_EXCEPTION):
                    err_count += 1
                if 0 <= ctx.max_work_count <= work_count \
                        or 0 <= ctx.max_err_count < err_count:
                    response.add_action(ACT_RESTART)
                    self._exit = True
                    break
                response_queue.put(response)
                response = None
            if self._exit:
                break
        for name, task in async_tasks.items():
            if not task.done():
                await task
        if response is not None and response.action != ACT_NONE:
            response_queue.put(response)
        while not async_response_queue.empty():
            response: _ActionItem = await async_response_queue.get()
            response_queue.put(response)
        self._running = False

    def start(self):
        if not self._exit:
            raise RuntimeError(f'AsyncWorker {self._name} is already started')
        self._thread = threading.Thread(target=self.run,
                                        daemon=self._daemon)
        self._thread.start()
        while self._exit:
            pass  # wait while self.loop is generating

    def run(self):
        self._async_loop = asyncio.new_event_loop()
        self._async_loop.run_until_complete(self._manager_coroutine())

    def stop(self):
        self._exit = True
        if self._thread is None and not self._running:
            return
        if self._thread.is_alive():
            self._thread.join()
        self._thread = None


class _ThreadWorker:

    def __init__(self, ctx: _ThreadWorkerContext):
        self._work_queue = ctx.work_queue
        self._name = ctx.name
        self._daemon = ctx.daemon
        self._wait_timeout = ctx.wait_interval
        self.ctx = ctx

        self._running: bool = False
        self._exit: bool = True
        self._thread: Optional[threading.Thread] = None

        self._idle: bool = True
        self._work_id: Optional[Hashable] = None

    def _enter_response_ctx(self, work_id: Hashable = None):
        self._idle = False
        self._work_id = work_id

    def _exit_response_ctx(self):
        self._idle = True
        self._work_id = None

    def _get_response(
            self,
            action: int = ACT_NONE,
            result: Any = None,
            exception: BaseException = None
    ) -> _ActionItem:
        return _ActionItem(action=action,
                           work_id=self._work_id,
                           worker_id=self._name,
                           result=result,
                           exception=exception)

    def run(self):
        self._exit = False
        ctx = self.ctx
        work_queue = ctx.work_queue
        request_queue = ctx.request_queue
        response_queue = ctx.response_queue
        idle_timeout = ctx.idle_timeout
        wait_interval = ctx.wait_interval

        get_response = self._get_response
        enter_response_ctx = self._enter_response_ctx
        exit_response_ctx = self._exit_response_ctx

        work_count: int = 0
        err_count: int = 0
        cons_err_count: int = 0

        response: Optional[_ActionItem] = None
        self._running = True
        idle_tick = time.monotonic()

        while True:
            if time.monotonic() - idle_tick > idle_timeout:
                response = get_response(ACT_CLOSE | ACT_TIMEOUT)
                break
            while not request_queue.empty():
                request: _ActionItem = request_queue.get()
                if request.match(ACT_RESET):
                    work_count = 0
                    err_count = 0
                    cons_err_count = 0
                if request.match(ACT_CMD_WORKER_STOP_FLAGS):
                    response = get_response(request.action)
                    self._exit = True
                    break
            if self._exit:
                break
            try:  # Wait for new work to execute
                call_item: _CallItem = work_queue.get(timeout=wait_interval)
            except Empty:
                continue
            result = None
            try:
                enter_response_ctx(call_item.name)
                # Check if future is cancelled
                if call_item.cancelled:
                    raise CancelledError(f'Future "{self._work_id}" has been cancelled')
                result = call_item.func(*call_item.args, **call_item.kwargs)
            except Exception as exc:
                err_count += 1
                cons_err_count += 1
                response = get_response(ACT_EXCEPTION, result=result, exception=exc)
            else:
                cons_err_count = 0
                response = get_response(ACT_DONE, result=result)
            finally:
                work_count += 1
                exit_response_ctx()
                idle_tick = time.monotonic()
                if 0 <= ctx.max_work_count <= work_count \
                        or 0 <= ctx.max_err_count < err_count \
                        or 0 <= ctx.max_cons_err_count < cons_err_count:
                    response.add_action(ACT_RESTART)
                    break
                response_queue.put(response)
                response = None
        if response is not None and response.action != ACT_NONE:
            response_queue.put(response)
        self._running = False

    def idle(self) -> bool:
        return self._idle

    def start(self):
        if not self._exit:
            raise RuntimeError(f'ThreadWorker {self._name} is already started')
        self._thread = threading.Thread(
            target=self.run,
            daemon=self._daemon
        )
        self._thread.start()
        while self._exit:
            pass

    def stop(self):
        self._exit = True
        if self._thread and self._thread.is_alive():
            self._thread.join()
        self._running = False
        self._thread = None


def _process_worker(
        name: Hashable,
        work_queue: mp.Queue,
        request_queue: mp.Queue,
        response_queue: mp.Queue,
        idle_flag: mp.Event,
        idle_timeout: float,
        wait_interval: float,
        max_work_count: int,
        max_err_count: int,
        max_cons_err_count: int
):
    worker_id = name
    work_id: Optional[Hashable] = None

    def get_response(
            action: int = ACT_NONE,
            result: Any = None,
            exception: BaseException = None
    ) -> _ActionItem:
        return _ActionItem(action=action,
                           work_id=work_id,
                           worker_id=worker_id,
                           result=result,
                           exception=exception)

    work_count = 0
    err_count = 0
    cons_err_count = 0

    response: Optional[_ActionItem] = None
    exit_ = False
    idle_tick = time.monotonic()

    while True:
        if time.monotonic() - idle_tick > idle_timeout:
            response = get_response(ACT_CLOSE)
            break
        while not request_queue.empty():
            request: _ActionItem = request_queue.get()
            # Clear the idle flag
            if idle_flag.is_set():
                idle_flag.clear()
            if request.match(ACT_RESET):
                work_count = 0
                err_count = 0
                cons_err_count = 0
            if request.match(ACT_CMD_WORKER_STOP_FLAGS):
                response = get_response(ACT_CLOSE)
                idle_flag.clear()  # mark as busy when closing
                exit_ = True
                break
        if exit_:
            break
        try:  # Wait for new work to execute
            call_item: _CallItem = work_queue.get(timeout=wait_interval)
        except Empty:
            continue
        result = None
        try:
            # Clear the idle flag
            if idle_flag.is_set():
                idle_flag.clear()
            work_id = call_item.name  # equals to enter_response_ctx
            # Check if future is cancelled
            if call_item.cancelled:
                raise CancelledError(f'Future "{work_id}" has been cancelled')
            result = call_item.func(*call_item.args, **call_item.kwargs)
        except Exception as exc:
            err_count += 1
            cons_err_count += 1
            response = get_response(ACT_EXCEPTION, result=result, exception=exc)
        else:
            cons_err_count = 0
            response = get_response(ACT_DONE, result=result)
        finally:
            work_count += 1
            work_id = None  # equals to exit_response_ctx
            del call_item
            idle_tick = time.monotonic()
            if 0 <= max_work_count <= work_count \
                    or 0 <= max_err_count < err_count \
                    or 0 <= max_cons_err_count < cons_err_count:
                response.add_action(ACT_CLOSE)
                idle_flag.clear()  # mark as busy when closing
                break
            response_queue.put(response)
            response = None
            idle_flag.set()  # mark as idle when a task is finished
    if response is not None and response.action != ACT_NONE:
        response_queue.put(response)


class _ProcessWorker:

    def __init__(self, ctx: _ProcessWorkerContext):
        self.ctx = ctx
        self._process: Optional[mp.Process] = None

        # As there is a time interval between start() invoked and got first task from task queue,
        # _ProcessWorker needs a flag to indicate it is still initializing and "idle", so as to prevent
        # manager worker from creating redundant process workers.
        # Also, there is a time interval between stop() invoked and worker deconstructed, _ProcessWorker
        # needs a flag to tell manager worker when it is safe to omit deconstruction duration and create
        # another worker
        self._idle = mp.Event()
        self._idle.set()

    @property
    def sentinel(self):
        return self._process.sentinel if self._process else None

    def idle(self) -> bool:
        # A ProcessWorker should be marked as idle while being constructed and busy while being deconstructed
        return self._idle.is_set()

    def start(self):
        if self._process is not None:
            if self._process.is_alive():
                raise RuntimeError(f'ProcessWorker {self.ctx.name} is already running')
            else:
                self._process.close()
                self._process = None
        ctx = self.ctx

        self._process = ctx.process_context.Process(
            target=_process_worker,
            kwargs={
                'name': ctx.name,
                'work_queue': ctx.work_queue,
                'request_queue': ctx.request_queue,
                'response_queue': ctx.response_queue,
                'idle_flag': self._idle,
                'idle_timeout': ctx.idle_timeout,
                'wait_interval': ctx.wait_interval,
                'max_work_count': ctx.max_work_count,
                'max_err_count': ctx.max_err_count,
                'max_cons_err_count': ctx.max_cons_err_count
            },
            daemon=ctx.daemon
        )
        self._process.start()

    def stop(self):
        self._idle.clear()
        if self._process is None:
            return
        if self._process.is_alive():
            self._process.join()
        self._process.close()
        self._process = None


def _worker_manager(
        executor_ref: ReferenceType = None,
        thread_workers: Dict[Hashable, _ThreadWorker] = None,
        process_workers: Dict[Hashable, _ProcessWorker] = None,
        async_workers: Dict[Hashable, _AsyncWorker] = None,
        work_items: Dict[Hashable, _WorkItem] = None,
        thread_work_queue: SimpleQueue = None,
        thread_response_queue: SimpleQueue = None,
        process_work_queue: mp.Queue = None,
        process_response_queue: mp.Queue = None,
        async_work_queue: SimpleQueue = None,
        async_response_queue: SimpleQueue = None,
        stop_event: threading.Event = None,
        max_thread_workers: int = 4,
        max_process_workers: int = 1,
        max_async_workers: int = -1,
        thread_worker_id_prefix: str = 'ThreadWorker-',
        process_worker_id_prefix: str = 'ProcessWorker-',
        async_worker_id_prefix: str = 'AsyncWorker-',
        incremental_thread_pool: bool = True,
        incremental_process_pool: bool = True,
        enable_thread: bool = True,
        enable_process: bool = True,
        enable_async: bool = True,
        wait_interval: float = 0.1,
        max_thread_actions: int = 10,
        max_process_actions: int = 10,
        max_async_actions: int = 10
):
    # TODO: Make max_thread_actions and max_process_actions more dynamic based on number of work/workers/cpu_count.

    def chaos_error(*args, **kwargs):
        raise RuntimeError()

    adjust_thread_workers \
        = adjust_process_workers \
        = adjust_async_worker \
        = chaos_error
    global _global_shutdown

    def adjust_iterator(
            workers: Dict[Hashable, Union[_ThreadWorker, _ProcessWorker]],
            work_queue: Union[SimpleQueue, mp.Queue],
            curr_workers: int,
            max_workers: int
    ) -> range:
        idle_workers: int = sum(1 if w.idle() else 0 for w in workers.values())
        qsize = work_queue.qsize()
        if max_workers < 0:
            iterator = range(qsize - idle_workers)
        else:
            iterator = range(curr_workers, min(max_workers, curr_workers + qsize - idle_workers))
        return iterator

    if enable_async:
        async_worker_id = f'{async_worker_id_prefix}0'

        def get_default_async_ctx(id_: Hashable):
            return _AsyncWorkerContext(
                name=id_,
                work_queue=async_work_queue,
                request_queue=SimpleQueue(),
                response_queue=async_response_queue,
                daemon=True,
                idle_timeout=60.0,
                wait_interval=0.1,
                max_work_count=max_async_workers,
                max_err_count=3
            )

        def adjust_async_worker():
            # Only keep one AsyncWorker at same time
            if len(async_workers) == 0:
                async_worker = _AsyncWorker(get_default_async_ctx(async_worker_id))
                async_workers[async_worker_id] = async_worker
                async_worker.start()

    if enable_thread:
        next_thread_worker_id = itertools.count().__next__
        static_thread_pool: bool = False

        def get_next_thread_worker_id():
            while (id_ := f'{thread_worker_id_prefix}{next_thread_worker_id()}') not in thread_workers:
                return id_

        def get_default_thread_ctx(id_: Hashable):
            return _ThreadWorkerContext(
                name=id_,
                work_queue=thread_work_queue,
                request_queue=SimpleQueue(),
                response_queue=thread_response_queue,
                daemon=True,
                idle_timeout=60.0,
                wait_interval=0.1,
                max_work_count=12,
                max_err_count=3,
                max_cons_err_count=-1
            )

        def adjust_thread_workers():
            nonlocal static_thread_pool
            curr_workers = len(thread_workers)
            if static_thread_pool or curr_workers == max_thread_workers:
                return
            if incremental_thread_pool or max_thread_workers < 0:
                for _ in adjust_iterator(thread_workers,
                                         thread_work_queue,
                                         curr_workers,
                                         max_thread_workers):
                    id_ = get_next_thread_worker_id()
                    thread_worker = _ThreadWorker(get_default_thread_ctx(id_))
                    thread_workers[id_] = thread_worker
                    thread_worker.start()
            else:
                for _ in range(max_thread_workers):
                    id_ = get_next_thread_worker_id()
                    thread_worker = _ThreadWorker(get_default_thread_ctx(id_))
                    thread_workers[id_] = thread_worker
                    thread_worker.start()
                static_thread_pool = True

    if enable_process:
        next_process_worker_id = itertools.count().__next__
        static_process_pool: bool = False

        def get_next_process_worker_id():
            while (id_ := f'{process_worker_id_prefix}{next_process_worker_id()}') not in process_workers:
                return id_

        mp_ctx = mp.get_context()

        def get_default_process_ctx(id_: Hashable):
            return _ProcessWorkerContext(
                name=id_,
                work_queue=process_work_queue,
                request_queue=mp.Queue(),
                response_queue=process_response_queue,
                process_context=mp_ctx,
                daemon=False,  # Set to False to allow child process.
                idle_timeout=60.0,
                wait_interval=0.1,
                # Not recommended to set max_work_count > 1 as main thread context
                # may be changed and differ from long-live process workers.
                max_work_count=1,
                max_err_count=-1,
                max_cons_err_count=-1
            )

        def adjust_process_workers():
            nonlocal static_process_pool
            curr_workers = len(process_workers)
            if static_process_pool or curr_workers == max_process_workers:
                return
            if incremental_process_pool or max_process_workers < 0:
                for _ in adjust_iterator(process_workers,
                                         process_work_queue,
                                         curr_workers,
                                         max_process_workers):
                    id_ = get_next_process_worker_id()
                    process_worker = _ProcessWorker(get_default_process_ctx(id_))
                    process_workers[id_] = process_worker
                    process_worker.start()
            else:
                for _ in range(max_process_workers):
                    id_ = get_next_process_worker_id()
                    process_worker = _ProcessWorker(get_default_process_ctx(id_))
                    process_workers[id_] = process_worker
                    process_worker.start()
                static_process_pool = True

    def consume_response(
            response: _ActionItem,
            workers: Dict[Hashable, Union[_ThreadWorker, _ProcessWorker, _AsyncWorker]]
    ):
        work_id = response.work_id
        worker_id = response.worker_id
        if response.match(ACT_DONE) or response.match(ACT_EXCEPTION):
            work_item = work_items.pop(work_id)  # get and delete the work from worker_items
            # Double check if future is cancelled
            if work_item.future.cancelled:
                work_item.future.set_result(result=None,
                                            exception=CancelledError(f'Future "{work_id}" has been cancelled'))
            else:
                work_item.future.set_result(response.result, response.exception)
        if response.match(ACT_CLOSE):
            workers.pop(worker_id)
        elif response.match(ACT_RESTART):
            worker = workers[worker_id]
            worker.stop()
            worker.start()

    def stop_workers():
        stop_action = _ActionItem(ACT_CLOSE)
        if enable_thread:
            for _, worker in thread_workers.items():
                worker.ctx.request_queue.put(stop_action)
        if enable_process:
            for _, worker in process_workers.items():
                worker.ctx.request_queue.put(stop_action)
        if enable_async:
            for _, worker in async_workers.items():
                worker.ctx.request_queue.put(stop_action)
        if enable_thread:
            for name, worker in thread_workers.items():
                worker.stop()
        if enable_process:
            for _, worker in process_workers.items():
                worker.stop()
        if enable_async:
            for _, worker in async_workers.items():
                worker.stop()

    while True:
        executor: Optional[HybridPoolExecutor] = executor_ref()
        if executor is None:
            break

        if enable_thread:
            adjust_thread_workers()
        if enable_process:
            adjust_process_workers()
        if enable_async:
            adjust_async_worker()

        if stop_event.is_set():
            break
        if _global_shutdown:
            break

        if len(work_items) == 0:
            stop_event.wait(wait_interval)
            continue

        processed: bool = False
        if enable_thread:
            thread_actions_count = 0
            while not thread_response_queue.empty():
                processed = True
                response = thread_response_queue.get()
                consume_response(response, thread_workers)
                thread_actions_count += 1
                if thread_actions_count >= max_thread_actions:
                    break
        if enable_process:
            process_actions_count = 0
            while not process_response_queue.empty():
                processed = True
                consume_response(process_response_queue.get(), process_workers)
                process_actions_count += 1
                if process_actions_count >= max_process_actions:
                    break
        if enable_async:
            async_actions_count = 0
            while not async_response_queue.empty():
                processed = True
                consume_response(async_response_queue.get(), async_workers)
                async_actions_count += 1
                if async_actions_count >= max_async_actions:
                    break
        if not processed:
            stop_event.wait(wait_interval)
    stop_workers()


class HybridPoolExecutor:
    _next_work_seq = itertools.count().__next__
    _next_thread_worker_seq = itertools.count().__next__
    _next_process_worker_seq = itertools.count().__next__

    def __init__(
            self,
            thread_workers: int = 0,
            process_workers: int = 0,
            async_workers: int = 0,
            incremental_thread_pool: bool = True,
            incremental_process_pool: bool = True,
            work_name_prefix: Hashable = 'Work-',
            thread_worker_name_prefix: Hashable = 'ThreadWorker-',
            process_worker_name_prefix: Hashable = 'ProcessWorker-',
            async_worker_name_prefix: Hashable = 'AsyncWorker-',
            redirect_thread: ThreadFallbackMode = None,
            redirect_process: ProcessFallbackMode = None,
            redirect_async: AsyncFallbackMode = None
    ):
        self._enable_thread = self._enable_process = self._enable_async = True
        self._mode_redirects = {
            WORK_MODE_THREAD: redirect_thread or WORK_MODE_THREAD,
            WORK_MODE_PROCESS: redirect_process or WORK_MODE_PROCESS,
            WORK_MODE_ASYNC: redirect_async or WORK_MODE_ASYNC,
            WORK_MODE_LOCAL: WORK_MODE_LOCAL
        }
        values = self._mode_redirects.values()
        if WORK_MODE_THREAD not in values:
            self._enable_thread = False
        if WORK_MODE_PROCESS not in values:
            self._enable_process = False
        if WORK_MODE_ASYNC not in values:
            self._enable_async = False

        # async
        # Param max_async_workers is actually a logic value to control how many coroutines
        # is running in one single AsyncWorker.
        if async_workers is None or async_workers <= 0:  # Unlimited
            self._max_async_workers = -1
        else:
            self._max_async_workers = async_workers
        self._async_worker_id_prefix = async_worker_name_prefix
        self._async_workers: Dict[Hashable, _AsyncWorker] = {}
        if self._enable_async:
            self._async_work_queue = SimpleQueue()
            self._async_response_queue = SimpleQueue()
        else:
            self._async_work_queue = None
            self._async_response_queue = None

        # thread
        if thread_workers in (None, 0):
            self._max_thread_workers = min(MAX_THREADS, (os.cpu_count() or 1) + 4)
        elif thread_workers < 0:  # Unlimited
            self._max_thread_workers = -1
        else:
            self._max_thread_workers = thread_workers
        self._incremental_thread_pool = incremental_thread_pool
        self._thread_worker_id_prefix = thread_worker_name_prefix
        self._thread_workers: Dict[Hashable, _ThreadWorker] = {}
        if self._enable_thread:
            self._thread_work_queue = SimpleQueue()
            self._thread_response_queue = SimpleQueue()
        else:
            self._thread_work_queue = None
            self._thread_response_queue = None

        # process
        if process_workers in (None, 0):
            self._max_process_workers = min(MAX_PROCESSES, os.cpu_count() or 1)
        elif process_workers < 0:  # Unlimited
            self._max_process_workers = -1
        else:
            self._max_process_workers = process_workers
        self._incremental_process_pool = incremental_process_pool
        self._process_worker_id_prefix = process_worker_name_prefix
        self._process_workers: Dict[Hashable, _ProcessWorker] = {}
        if self._enable_process:
            self._process_work_queue = mp.Queue()
            self._process_response_queue = mp.Queue()
        else:
            self._process_work_queue = None
            self._process_response_queue = None

        self._work_items: Dict[Hashable, _WorkItem] = {}
        self._work_name_prefix = work_name_prefix
        self._management_thread: Optional[threading.Thread] = None
        self._stop_event: threading.Event = threading.Event()

    def submit(
            self,
            func: Callable[..., Any],
            args: Tuple[Any, ...] = (),
            kwargs: Dict[str, Any] = None,
            name: Hashable = None,
            mode: WorkMode = None
    ) -> PollFuture:
        if not mode:
            if asyncio.iscoroutinefunction(func):
                mode = WORK_MODE_ASYNC
            else:
                mode = WORK_MODE_THREAD
        if mode not in work_modes:
            raise ValueError(f'Unrecognized mode "{mode}".')
        if kwargs is None:
            kwargs = {}
        if name is None:
            while (n := f'{self._work_name_prefix}{HybridPoolExecutor._next_work_seq()}') \
                    not in self._work_items:
                name = n
                break
        elif name in self._work_items:
            raise KeyError(f'Work name "{name}" exists')
        _, future = self._gen_work_item_and_future(func, args, kwargs, name, mode)
        self._wakeup_worker_manager()

        return future

    def _gen_work_item_and_future(
            self,
            func: Callable[..., Any],
            args: Tuple[Any, ...],
            kwargs: Dict[str, Any],
            name: Hashable,
            mode: WorkMode
    ) -> Tuple[_WorkItem, PollFuture]:
        call_item = _CallItem(name, func, args, kwargs)
        future = PollFuture(name, call_item)
        work_item = _WorkItem(name, future, call_item)
        self._work_items[name] = work_item
        mode = self._mode_redirects[mode]
        is_async_func = asyncio.iscoroutinefunction(func)
        if is_async_func and mode not in (WORK_MODE_ASYNC, WORK_MODE_LOCAL):
            warnings.warn(f'Work "{name}" ({func.__name__}) is an async function but set to be running under '
                          f'"{mode}" mode, coercing to "async" mode instead.',
                          RuntimeWarning, stacklevel=2)
            mode = WORK_MODE_ASYNC
        elif mode == WORK_MODE_ASYNC and not is_async_func:
            warnings.warn(f'Work "{name}" ({func.__name__}) is an async function but set to be running under '
                          '"async" mode, coercing to "thread" mode instead.',
                          RuntimeWarning, stacklevel=2)
            mode = WORK_MODE_THREAD
        if mode == WORK_MODE_THREAD:
            self._thread_work_queue.put(call_item)
        elif mode == WORK_MODE_PROCESS:
            self._process_work_queue.put(call_item)
        elif mode == WORK_MODE_ASYNC:
            self._async_work_queue.put(call_item)
        elif mode == WORK_MODE_LOCAL:
            self._exec_local(call_item, future)
        return work_item, future

    @classmethod
    def _exec_local(cls, call_item: _CallItem, future: PollFuture):
        result = None
        # Check if future is cancelled
        if future.cancelled:
            future.set_result(exception=CancelledError())
            return
        # FIXME: RuntimeWarning: coroutine never awaited in 'async' mode due to set_async in notify_async_event
        # This issue only occurs when mode='local' and is_coroutine == True
        try:
            if asyncio.iscoroutinefunction(call_item.func):
                result = asyncio.run(call_item.func(*call_item.args, **call_item.kwargs))
            else:
                result = call_item.func(*call_item.args, **call_item.kwargs)
            # Double check if future is cancelled
            if future.cancelled:
                raise CancelledError(f'Future "{future.name}" has been cancelled')
        except Exception as exc:
            future.set_result(result=result, exception=exc)
        else:
            future.set_result(result=result)

    def _wakeup_worker_manager(self):
        if self._management_thread is None:
            def shutdown_cb(executor_ref: ReferenceType):
                executor = executor_ref()
                if executor:
                    executor.shutdown()

            self._management_thread = threading.Thread(
                target=_worker_manager,
                name='ThreadManager',
                kwargs={
                    'executor_ref': weakref.ref(self, shutdown_cb),
                    'thread_workers': self._thread_workers,
                    'process_workers': self._process_workers,
                    'async_workers': self._async_workers,
                    'work_items': self._work_items,
                    'thread_work_queue': self._thread_work_queue,
                    'thread_response_queue': self._thread_response_queue,
                    'process_work_queue': self._process_work_queue,
                    'process_response_queue': self._process_response_queue,
                    'async_work_queue': self._async_work_queue,
                    'async_response_queue': self._async_response_queue,
                    'stop_event': self._stop_event,
                    'max_thread_workers': self._max_thread_workers,
                    'max_process_workers': self._max_process_workers,
                    'max_async_workers': self._max_async_workers,
                    'thread_worker_id_prefix': self._thread_worker_id_prefix,
                    'process_worker_id_prefix': self._process_worker_id_prefix,
                    'async_worker_id_prefix': self._async_worker_id_prefix,
                    'incremental_thread_pool': self._incremental_thread_pool,
                    'incremental_process_pool': self._incremental_process_pool,
                    'enable_thread': self._enable_thread,
                    'enable_process': self._enable_process,
                    'enable_async': self._enable_async
                },
                daemon=True
            )
            self._management_thread.start()
            _manager_threads.add(self._management_thread)

    def shutdown(self):
        if self._management_thread is not None:
            self._stop_event.set()
            self._management_thread.join()
            self._management_thread = None
