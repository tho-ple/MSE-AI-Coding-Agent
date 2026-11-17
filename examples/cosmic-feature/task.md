Feature: let customers cancel an allocated order line (Cosmic Python allocation service).

Add a new command `Deallocate` with the fields `orderid`, `sku` and `qty` (same style as the
existing `Allocate` command). When the message bus handles it, the order line is removed from the
batch it was allocated to, so that stock becomes available again, and the unit of work commits.

Rules:
1. The domain model decides: `Product.deallocate(line)` removes the order line from the batch that
   holds it, increments the product's `version_number`, and returns that batch's reference.
2. If the order line is not allocated in any batch of the product, raise a new domain exception
   `NotAllocated` (defined next to the domain model) whose message contains the order id.
   Nothing changes and nothing is committed.
3. If the SKU is unknown, raise the existing `InvalidSku` exception ("Invalid sku <sku>").
4. Deallocating must NOT trigger a reallocation: do not record a `Deallocated` event for it
   (that event makes the service re-allocate the line).
5. Existing commands, handlers and callers (`Allocate`, `CreateBatch`, `ChangeBatchQuantity`)
   must keep working unchanged.

Find the modules that define commands, the domain model and the service-layer handlers yourself.
You may add unit tests under `tests/unit/`. Run the unit tests with run_command name="unit_tests".
