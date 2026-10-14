Sending `commands.ChangeBatchQuantity` for a batch reference that does not exist crashes with
`AttributeError: 'NoneType' object has no attribute 'change_batch_quantity'`.

Fix it in `src/allocation/service_layer/handlers.py`: add an exception class `InvalidBatchRef`
next to the existing `InvalidSku`, and make the `change_batch_quantity` handler raise
`InvalidBatchRef(f"Invalid batch ref {cmd.ref}")` when no product has that batch.
Nothing may be committed in that case. Then run the unit tests (run_command name="unit_tests")
and call finish.
