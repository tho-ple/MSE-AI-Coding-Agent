Bug: changing the quantity of a batch is not validated (Cosmic Python allocation service).

1. Sending `commands.ChangeBatchQuantity` for a batch reference that does not exist crashes
   with `AttributeError: 'NoneType' object has no attribute 'change_batch_quantity'`.
   Expected: the handler `change_batch_quantity` in `src/allocation/service_layer/handlers.py`
   raises a new exception `handlers.InvalidBatchRef` with the message `Invalid batch ref <ref>`
   (same style as the existing `InvalidSku`). Nothing is committed.

2. A negative quantity is accepted and silently deallocates every order line of the batch.
   Expected: `Product.change_batch_quantity` in `src/allocation/domain/model.py` raises a new
   exception `model.InvalidQuantity` (defined in that module) when `qty < 0`, before changing
   anything: the batch keeps its quantity and allocations, and no events are recorded.

Only change `src/allocation/domain/model.py`, `src/allocation/service_layer/handlers.py`
and, if you want to add tests, files under `tests/unit/`. Existing behaviour must keep working:
run the unit tests with run_command name="unit_tests".
