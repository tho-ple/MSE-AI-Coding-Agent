"""Protected feature check: deallocating an order line (Cosmic Python allocation service).

Lives outside the agent's writable area; the harness copies it to the session's checks/
directory and runs it against the changed code. It defines its own fakes, so editing the
repository's tests cannot influence it.

On the starting commit every test fails because ``commands.Deallocate`` does not exist
(expected reason: ``AttributeError: module 'allocation.domain.commands' has no attribute 'Deallocate'``).
"""
import pytest

from allocation import bootstrap
from allocation.adapters import notifications, repository
from allocation.domain import commands, events, model
from allocation.service_layer import handlers, unit_of_work


class FakeRepository(repository.AbstractRepository):
    def __init__(self):
        super().__init__()
        self._products = set()

    def _add(self, product):
        self._products.add(product)

    def _get(self, sku):
        return next((p for p in self._products if p.sku == sku), None)

    def _get_by_batchref(self, batchref):
        return next((p for p in self._products for b in p.batches if b.reference == batchref), None)


class FakeUnitOfWork(unit_of_work.AbstractUnitOfWork):
    def __init__(self):
        self.products = FakeRepository()
        self.commits = 0

    def _commit(self):
        self.commits += 1

    def rollback(self):
        pass


class NoNotifications(notifications.AbstractNotifications):
    def send(self, destination, message):
        pass


def make_bus():
    return bootstrap.bootstrap(start_orm=False, uow=FakeUnitOfWork(),
                               notifications=NoNotifications(), publish=lambda *a: None)


def test_deallocate_makes_stock_available_again():
    bus = make_bus()
    bus.handle(commands.CreateBatch("b1", "LAMP", 100, None))
    bus.handle(commands.Allocate("o1", "LAMP", 10))
    commits = bus.uow.commits
    bus.handle(commands.Deallocate("o1", "LAMP", 10))
    [batch] = bus.uow.products.get("LAMP").batches
    # Also proves no reallocation happened (a Deallocated event would re-allocate o1).
    assert batch.available_quantity == 100
    assert bus.uow.commits == commits + 1


def test_deallocate_keeps_other_order_lines():
    bus = make_bus()
    bus.handle(commands.CreateBatch("b1", "LAMP", 100, None))
    bus.handle(commands.Allocate("o1", "LAMP", 10))
    bus.handle(commands.Allocate("o2", "LAMP", 25))
    bus.handle(commands.Deallocate("o1", "LAMP", 10))
    [batch] = bus.uow.products.get("LAMP").batches
    assert batch.available_quantity == 75


def test_domain_deallocate_returns_batchref_without_reallocation_event():
    line = model.OrderLine("o1", "LAMP", 10)
    early = model.Batch("early", "LAMP", 100, None)
    product = model.Product("LAMP", [early])
    product.allocate(line)
    product.events.clear()
    version = product.version_number

    assert product.deallocate(line) == "early"
    assert early.available_quantity == 100
    assert product.version_number == version + 1
    assert not any(isinstance(e, events.Deallocated) for e in product.events)


def test_deallocating_unallocated_line_raises_not_allocated():
    bus = make_bus()
    bus.handle(commands.CreateBatch("b1", "LAMP", 100, None))
    bus.handle(commands.Allocate("o1", "LAMP", 10))
    commits = bus.uow.commits
    with pytest.raises(model.NotAllocated, match="o9"):
        bus.handle(commands.Deallocate("o9", "LAMP", 10))
    [batch] = bus.uow.products.get("LAMP").batches
    assert batch.available_quantity == 90
    assert bus.uow.commits == commits


def test_deallocate_unknown_sku_raises_invalid_sku():
    bus = make_bus()
    with pytest.raises(handlers.InvalidSku, match="Invalid sku NOPE"):
        bus.handle(commands.Deallocate("o1", "NOPE", 1))


def test_existing_callers_still_work():
    bus = make_bus()
    bus.handle(commands.CreateBatch("b1", "LAMP", 50, None))
    bus.handle(commands.Allocate("o1", "LAMP", 20))
    bus.handle(commands.Allocate("o2", "LAMP", 20))
    bus.handle(commands.ChangeBatchQuantity("b1", 25))  # still deallocates + reallocates via events
    [batch] = bus.uow.products.get("LAMP").batches
    assert batch.available_quantity == 5
