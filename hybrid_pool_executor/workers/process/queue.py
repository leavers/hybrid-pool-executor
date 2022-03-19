import errno
import sys
import time
import typing as t
from multiprocessing import Value
from multiprocessing import context as _ctx
from multiprocessing import get_context
from multiprocessing.queues import Queue as BaseQueue
from multiprocessing.reduction import ForkingPickler
from multiprocessing.util import debug, info, is_exiting
from queue import Empty, Full

try:
    import cloudpickle

    _ForkingPickler = cloudpickle
except ImportError:
    _ForkingPickler = ForkingPickler


_sentinel = object()


class Queue(BaseQueue):
    def __init__(self, maxsize: int = 0, *, ctx: t.Optional[_ctx.BaseContext] = None):
        super().__init__(maxsize=maxsize, ctx=ctx or get_context())
        self._qsize = Value("L", 0)

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

    def qsize(self) -> int:
        return self._qsize.value

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

    @staticmethod
    def _feed(
        buffer, notempty, send_bytes, writelock, close, ignore_epipe, onerror, queue_sem
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
                    self._qsize.value -= 1
                    onerror(e, obj)
