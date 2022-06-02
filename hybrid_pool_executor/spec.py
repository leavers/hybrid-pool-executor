import importlib
import typing as t

from hybrid_pool_executor.base import ExistsError, ModuleSpec
from hybrid_pool_executor.utils import SingletonMeta

_default_modules = [
    "hybrid_pool_executor.workers.asyncio",
    "hybrid_pool_executor.workers.process",
    "hybrid_pool_executor.workers.thread",
]


class ModuleSpecRepo(dict):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._tag_index: t.Dict[str, t.Set[str]] = {}

    def import_spec(self, spec: ModuleSpec, overwrite: bool = False):
        name = spec.name
        if name in self and not overwrite:
            raise ExistsError(
                f'Spec "{name}" already exists, set overwrite=True if you want to '
                "overwrite existing spec."
            )
        super().__setitem__(name, spec)
        for tag in spec.tags:
            if tag not in self._tag_index:
                self._tag_index[tag] = set()
            index = self._tag_index[tag]
            index.add(name)

    def __getitem__(self, name: str) -> ModuleSpec:
        return t.cast(ModuleSpec, super().__getitem__(name))

    def __setitem__(self, name: str, spec: ModuleSpec) -> None:
        if name != spec.name:
            raise ValueError(
                f'Key ("{name}") and spec name ("{spec.name}") does not match.'
            )
        return self.import_spec(spec=spec, overwrite=True)

    def filter_by_tags(self, *tags: str) -> t.Optional[t.FrozenSet[str]]:
        if not tags:
            return frozenset()
        filter: t.Set[str] = set()
        for tag in tags:
            index = self._tag_index.get(tag)
            if not index:
                return frozenset()
            if not filter:
                filter |= index
            else:
                filter &= index
        return frozenset(filter)


class ModuleSpecFactory(ModuleSpecRepo, metaclass=SingletonMeta):
    def __init__(self):
        super().__init__()
        # load default module specs
        self._import_default()

    def _import_default(self):
        for module in _default_modules:
            self.import_module(module)

    def import_module(self, module: str):
        module_spec: ModuleSpec = t.cast(
            ModuleSpec, importlib.import_module(module).MODULE_SPEC
        )
        self.import_spec(module_spec)

    def get_repo(self) -> ModuleSpecRepo:
        repo = ModuleSpecRepo()
        for spec in self.values():
            repo.import_spec(spec)
        return repo


spec_factory = ModuleSpecFactory()
