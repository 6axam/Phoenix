"""Built-in application adapters and their small registry."""
from .base import AdapterRegistry, AdapterResult, PlanStep, Status
from .kitty import KittyAdapter
from .generic import GenericAdapter


def builtin_adapters(settings):
    registry = AdapterRegistry()
    registry.register(KittyAdapter(settings))
    registry.register(GenericAdapter())
    return registry
