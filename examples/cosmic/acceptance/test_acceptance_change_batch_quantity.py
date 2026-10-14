"""Acceptance check for the ChangeBatchQuantity validation task.

Lives outside the agent's writable area; the harness copies it to the run's
checks/ directory and runs it against the modified code. It defines its own
fakes so the agent cannot influence it by editing the repository's tests.
"""
import pytest

from allocation import bootstrap
from allocation.adapters import notifications, repository
from allocation.domain import commands, model
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


def test_unknown_batch_ref_raises_invalid_batch_ref():
    bus = make_bus()
    bus.handle(commands.CreateBatch("b1", "LAMP", 100, None))
    commits = bus.uow.commits
    with pytest.raises(handlers.InvalidBatchRef, match="Invalid batch ref NOPE"):
        bus.handle(commands.ChangeBatchQuantity("NOPE", 10))
    assert bus.uow.commits == commits


def test_negative_quantity_rejected_by_domain_model():
    line = model.OrderLine("o1", "LAMP", 10)
    batch = model.Batch("b1", "LAMP", 100, None)
    product = model.Product("LAMP", [batch])
    product.allocate(line)
    product.events.clear()

    with pytest.raises(model.InvalidQuantity):
        product.change_batch_quantity("b1", -5)

    assert batch.available_quantity == 90
    assert product.events == []


def test_negative_quantity_via_message_bus_keeps_allocations():
    bus = make_bus()
    bus.handle(commands.CreateBatch("b1", "LAMP", 100, None))
    bus.handle(commands.Allocate("o1", "LAMP", 10))
    with pytest.raises(model.InvalidQuantity):
        bus.handle(commands.ChangeBatchQuantity("b1", -1))
    [batch] = bus.uow.products.get("LAMP").batches
    assert batch.available_quantity == 90


def test_valid_change_still_reallocates():
    bus = make_bus()
    bus.handle(commands.CreateBatch("b1", "LAMP", 50, None))
    bus.handle(commands.Allocate("o1", "LAMP", 20))
    bus.handle(commands.Allocate("o2", "LAMP", 20))
    bus.handle(commands.ChangeBatchQuantity("b1", 25))
    [batch] = bus.uow.products.get("LAMP").batches
    assert batch.available_quantity == 5
