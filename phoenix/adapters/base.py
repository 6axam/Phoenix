from __future__ import annotations
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Protocol


class Status(str, Enum):
    EXACT = 'exact'
    PARTIAL = 'partial'
    UNSUPPORTED = 'unsupported'
    FAILED = 'failed'
    SKIPPED = 'skipped'


@dataclass
class PlanStep:
    id: str
    action: str
    depends_on: list[str] = field(default_factory=list)
    status: str = 'available'
    requires_confirmation: bool = False
    detail: str = ''

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AdapterResult:
    status: Status
    data: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class ApplicationAdapter(Protocol):
    name: str
    def matches(self, window: dict[str, Any]) -> bool: ...
    def availability(self) -> AdapterResult: ...
    def collect(self, collect_text: bool = False, expected_pid: int | None = None) -> tuple[AdapterResult, list[dict[str, Any]]]: ...
    def validate(self, data: Any) -> bool: ...
    def serialize(self, data: dict[str, Any]) -> dict[str, Any]: ...
    def execute(self, data: dict[str, Any], launch: Any) -> AdapterResult: ...
    def plan(self, data: dict[str, Any]) -> list[PlanStep]: ...


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ApplicationAdapter] = {}

    def register(self, adapter: ApplicationAdapter) -> None:
        if adapter.name in self._adapters:
            raise ValueError('Duplicate adapter: ' + adapter.name)
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> ApplicationAdapter | None:
        return self._adapters.get(name)

    def for_window(self, window: dict[str, Any]) -> ApplicationAdapter | None:
        return next((a for a in self._adapters.values() if a.matches(window)), None)

    def diagnostics(self) -> dict[str, Any]:
        return {name: {'status': (result := adapter.availability()).status.value, 'warnings': result.warnings} for name, adapter in self._adapters.items()}
