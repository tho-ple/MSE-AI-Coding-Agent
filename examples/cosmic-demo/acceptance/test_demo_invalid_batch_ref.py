"""Demo acceptance check: changing the quantity of an unknown batch must fail cleanly.

Red on the starting commit (AttributeError deep inside the handler), green once the
service layer raises a domain-specific error. Lives outside the agent's writable area.
"""
import pytest

from allocation import bootstrap
from allocation.adapters import notifications, repository
from allocation.domain import commands
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
    with pytest.raises(handlers.InvalidBatchRef, match="Invalid batch ref NOPE"):
        bus.handle(commands.ChangeBatchQuantity("NOPE", 10))
    assert bus.uow.commits == 1  # only CreateBatch was committed


def test_known_batch_ref_still_changes_quantity():
    bus = make_bus()
    bus.handle(commands.CreateBatch("b1", "LAMP", 100, None))
    bus.handle(commands.ChangeBatchQuantity("b1", 40))
    [batch] = bus.uow.products.get("LAMP").batches
    assert batch.available_quantity == 40
