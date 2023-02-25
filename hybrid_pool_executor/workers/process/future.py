import multiprocessing as mp
import typing as t
from concurrent.futures._base import Future as BaseFuture
from concurrent.futures._base import _Waiter
from multiprocessing.synchronize import Condition
from uuid import UUID, uuid4


class ProcessFuture(BaseFuture):
    def __init__(self, uid: UUID, condition) -> None:
        print(condition, type(condition))
        self._uid = uid
        self._condition = condition
        self._waiters: t.List[_Waiter] = []
        self._done_callbacks: t.List[t.Callable[[BaseFuture], None]] = []

    @property
    def _state(self) -> t.Optional[str]:
        return _fut_factory.get_state(self._uid)

    @_state.setter
    def _state(self, state) -> None:
        _fut_factory.set_state(self._uid, state)

    @property
    def _result(self) -> t.Any:
        return _fut_factory.get_result(self._uid)

    @_result.setter
    def _result(self, result) -> None:
        _fut_factory.set_result(self._uid, result)

    @property
    def _exception(self) -> t.Optional[BaseException]:
        return _fut_factory.get_exception(self._uid)

    @_exception.setter
    def _exception(self, exc) -> None:
        _fut_factory.set_exception(self._uid, exc)

    def __del__(self) -> None:
        _fut_factory.remove_future(self._uid)


class _ProcessFutureFactory:
    def __init__(self) -> None:
        self.manager = mp.Manager()
        self.lock = self.manager.Lock()
        self.store: t.Dict[t.Tuple[UUID, bytes], t.Any] = self.manager.dict()

    def get_future(self) -> ProcessFuture:
        with self.lock:
            while (key := (uuid4(), b"c")) not in self.store:
                break
            condition = self.manager.Condition()
            self.store[key] = condition
        return ProcessFuture(uid=key[0], condition=condition)

    def get_condition(self, uid: UUID) -> t.Optional[Condition]:
        return self.store.get((uid, b"c"))

    def set_condition(self, uid: UUID, condition) -> None:
        self.store[(uid, b"c")] = condition

    def get_state(self, uid: UUID) -> t.Optional[str]:
        return self.store.get((uid, b"s"))

    def set_state(self, uid: UUID, state: str) -> None:
        self.store[(uid, b"s")] = state

    def get_result(self, uid: UUID) -> t.Any:
        return self.store.get((uid, b"r"))

    def set_result(self, uid: UUID, result: t.Any) -> None:
        self.store[(uid, b"r")] = result

    def get_exception(self, uid: UUID) -> t.Optional[BaseException]:
        return self.store.get((uid, b"e"))

    def set_exception(self, uid: UUID, exc: BaseException) -> None:
        self.store[(uid, b"e")] = exc

    def remove_future(self, uid: UUID) -> None:
        for sub_key in (b"s", b"e", b"r", b"c"):
            key = (uid, sub_key)
            if key in self.store:
                self.store.pop(key)

    def __del__(self) -> None:
        self.manager.shutdown()


_fut_factory = _ProcessFutureFactory()
