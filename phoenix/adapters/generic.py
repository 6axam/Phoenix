from __future__ import annotations
from typing import Any
from .base import AdapterResult, PlanStep, Status


class GenericAdapter:
    name = 'generic'
    def matches(self, window: dict[str, Any]) -> bool:
        return bool(window.get('class'))
    def availability(self) -> AdapterResult:
        return AdapterResult(Status.PARTIAL, warnings=['Application internals are not restored'])
    def collect(self, collect_text: bool = False, expected_pid: int | None = None) -> tuple[AdapterResult, list[dict[str, Any]]]:
        return AdapterResult(Status.UNSUPPORTED, warnings=['Application internals are unavailable']), []
    def validate(self, data: Any) -> bool:
        return isinstance(data, dict)
    def serialize(self, data: dict[str, Any]) -> dict[str, Any]:
        if not self.validate(data): raise ValueError('Invalid generic state')
        return dict(data)
    def execute(self, data: dict[str, Any], launch: Any) -> AdapterResult:
        return AdapterResult(Status.UNSUPPORTED, warnings=['Use trusted desktop launch'])
    def plan(self, data: dict[str, Any]) -> list[PlanStep]:
        return [PlanStep('workspace', 'workspace'), PlanStep('window', 'trusted application window', ['workspace'])]
