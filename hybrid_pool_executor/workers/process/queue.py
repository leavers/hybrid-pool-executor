import collections
import errno
import os
import sys
import threading
import time
import typing as t
import weakref
from multiprocessing import Value, connection
from multiprocessing import context as _ctx
from multiprocessing import get_context
from multiprocessing.queues import Queue as BaseQueue
from multiprocessing.reduction import ForkingPickler
from multiprocessing.util import Finalize, debug, info, is_exiting, register_after_fork
from queue import Empty, Full

try:
    import cloudpickle

    _ForkingPickler = cloudpickle
except ImportError:
    _ForkingPickler = ForkingPickler


_sentinel = object()


class Queue(BaseQueue):
    def __init__(self, maxsize: int = 0, *, ctx: t.Optional[_ctx.BaseContext] = None):
        if maxsize <= 0:
            # Can raise ImportError (see issues #3770 and #23400)
            from multiprocessing.synchronize import SEM_VALUE_MAX  # type: ignore

            maxsize = SEM_VALUE_MAX

        if not ctx:
            ctx = get_context()

        self._maxsize = maxsize
        self._reader, self._writer = connection.Pipe(duplex=False)
        self._rlock = ctx.Lock()
        self._opid = os.getpid()
        self._qsize = Value("L", 0)  # type: ignore
        if sys.platform == "win32":
            self._wlock = None
        else:
            self._wlock = ctx.Lock()
        self._sem = ctx.BoundedSemaphore(maxsize)
        # For use by concurrent.futures
        self._ignore_epipe = False
        self._reset()

        if sys.platform != "win32":
            register_after_fork(self, Queue._after_fork)

    def __getstate__(self):
        _ctx.assert_spawning(self)
        return (
            self._ignore_epipe,
            self._maxsize,
            self._reader,
            self._writer,
            self._rlock,
            self._wlock,
            self._sem,
            self._opid,
            self._qsize,
        )

    def __setstate__(self, state):
        (
            self._ignore_epipe,
            self._maxsize,
            self._reader,
            self._writer,
            self._rlock,
            self._wlock,
            self._sem,
            self._opid,
            self._qsize,
        ) = state
        self._reset()

    def _after_fork(self):
        debug("Queue._after_fork()")
        self._reset(after_fork=True)

    def _reset(self, after_fork=False):
        if after_fork:
            self._notempty._at_fork_reinit()
        else:
            self._notempty = threading.Condition(threading.Lock())
        self._buffer = collections.deque()
        self._thread = None
        self._jointhread = None
        self._joincancelled = False
        self._closed = False
        self._close = None
        self._send_bytes = self._writer.send_bytes
        self._recv_bytes = self._reader.recv_bytes
        self._poll = self._reader.poll

    def put(self, obj, block=True, timeout=None):
        if self._closed:
            raise ValueError(f"Queue {self!r} is closed")
        if not self._sem.acquire(block, timeout):
            raise Full
        self._qsize.value += 1

        with self._notempty:
            if self._thread is None:
                self._start_thread()
            self._buffer.append(obj)
            self._notempty.notify()

    def get(self, block=True, timeout=None):
        if self._closed:
            raise ValueError(f"Queue {self!r} is closed")
        if block and timeout is None:
            with self._rlock:
                res = self._recv_bytes()
            self._sem.release()
        else:
            if block:
                deadline = time.monotonic() + timeout
            if not self._rlock.acquire(block, timeout):
                raise Empty
            try:
                if block:
                    timeout = deadline - time.monotonic()
                    if not self._poll(timeout):
                        raise Empty
                elif not self._poll():
                    raise Empty
                res = self._recv_bytes()
                self._sem.release()
            finally:
                self._rlock.release()
        # unserialize the data after having released the lock
        result = _ForkingPickler.loads(res)
        self._qsize.value -= 1
        return result

    def qsize(self):
        # Raises NotImplementedError on Mac OSX because of broken sem_getvalue()
        # return self._maxsize - self._sem._semlock._get_value()
        return self._qsize.value

    def _start_thread(self):
        debug("Queue._start_thread()")

        # Start thread which transfers data from buffer to pipe
        self._buffer.clear()
        self._thread = threading.Thread(
            target=Queue._feed,
            args=(
                self._buffer,
                self._notempty,
                self._send_bytes,
                self._wlock,
                self._writer.close,
                self._ignore_epipe,
                self._on_queue_feeder_error,
                self._sem,
                self._qsize,
            ),
            name="QueueFeederThread",
        )
        self._thread.daemon = True

        debug("doing self._thread.start()")
        self._thread.start()
        debug("... done self._thread.start()")

        if not self._joincancelled:
            self._jointhread = Finalize(
                self._thread,
                Queue._finalize_join,
                [weakref.ref(self._thread)],
                exitpriority=-5,
            )

        # Send sentinel to the thread queue object when garbage collected
        self._close = Finalize(
            self, Queue._finalize_close, [self._buffer, self._notempty], exitpriority=10
        )

    @staticmethod
    def _finalize_join(twr):
        debug("joining queue thread")
        thread = twr()
        if thread is not None:
            thread.join()
            debug("... queue thread joined")
        else:
            debug("... queue thread already dead")

    @staticmethod
    def _finalize_close(buffer, notempty):
        debug("telling queue thread to quit")
        with notempty:
            buffer.append(_sentinel)
            notempty.notify()

    @staticmethod
    def _feed(
        buffer,
        notempty,
        send_bytes,
        writelock,
        close,
        ignore_epipe,
        onerror,
        queue_sem,
        qsize,
    ):
        debug("starting thread to feed data to pipe")
        nacquire = notempty.acquire
        nrelease = notempty.release
        nwait = notempty.wait
        bpopleft = buffer.popleft
        sentinel = _sentinel
        if sys.platform != "win32":
            wacquire = writelock.acquire
            wrelease = writelock.release
        else:
            wacquire = None

        while 1:
            try:
                nacquire()
                try:
                    if not buffer:
                        nwait()
                finally:
                    nrelease()
                try:
                    while 1:
                        obj = bpopleft()
                        if obj is sentinel:
                            debug("feeder thread got sentinel -- exiting")
                            close()
                            return

                        # serialize the data before acquiring the lock
                        obj = _ForkingPickler.dumps(obj)
                        if wacquire is None:
                            send_bytes(obj)
                        else:
                            wacquire()
                            try:
                                send_bytes(obj)
                            finally:
                                wrelease()
                except IndexError:
                    pass
            except Exception as e:
                if ignore_epipe and getattr(e, "errno", 0) == errno.EPIPE:
                    return
                # Since this runs in a daemon thread the resources it uses
                # may be become unusable while the process is cleaning up.
                # We ignore errors which happen after the process has
                # started to cleanup.
                if is_exiting():
                    info("error in queue thread: %s", e)
                    return
                else:
                    # Since the object has not been sent in the queue, we need
                    # to decrease the size of the queue. The error acts as
                    # if the object had been silently removed from the queue
                    # and this step is necessary to have a properly working
                    # queue.
                    queue_sem.release()
                    qsize.value -= 1
                    onerror(e, obj)

    @staticmethod
    def _on_queue_feeder_error(e, obj):
        """
        Private API hook called when feeding data in the background thread
        raises an exception.  For overriding by concurrent.futures.
        """
        import traceback

        traceback.print_exc()
