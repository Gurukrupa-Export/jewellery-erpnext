# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""C09 -- one resident component of a mixed batch, with its owner attached.

WHY THIS EXISTS WHEN ``Batch.custom_origin_entries`` ALREADY DOES SOMETHING SIMILAR
----------------------------------------------------------------------------------
``custom_origin_entries`` (the ``Batch MultiSelect`` child table, written by
``customization/serial_and_batch_bundle/doc_events/utils.py:101-136``) is real, populated
provenance -- 4,198,721 rows on ``gk`` -- and it already distinguishes alloy from metal
downstream in ``customization/batch/batch.py:277-328``. It is deliberately NOT extended here,
and not derived from either. Six measured reasons:

1. **No owner column.** Ownership is recoverable only by joining the source ``Batch``'s
   *mutable* ``custom_inventory_type`` / ``custom_customer``. Re-tagging a batch therefore
   rewrites the history of every batch descended from it. This table stores the owner AS AT
   the moment of mixing, so later edits cannot reach backwards.
2. **De-dupe keys on ``batch_no`` alone** (``utils.py:122-131``), so a source batch consumed
   by two outward rows contributes a single row carrying a single qty.
3. **No apportionment.** When one lane produces two inward rows, both receive the FULL lane
   source list, so quantity double-counts.
4. **``qty`` is source-consumed, not component-resident** -- nothing ever decrements it.
5. **Not transitive.** A reverse conversion records its origin as the mixed batch, not that
   batch's components. CG-T083 needs exactly the transitive answer, so this table resolves
   recursively.
6. **No fine-gold column.**

And it is noisy: a real parent sampled on ``gk`` carries 170+ origin rows, most repeating the
same source batch with sub-0.01 g quantities. Anything derived from it would inherit that.

``rate`` and ``amount`` are written only under the Nominal valuation policy, for the same
reason as ``Customer Gold Ledger Entry``'s value fields: under Zero Value there is no value to
record, and a 0.0 would assert a measurement nobody took.
"""

from frappe.model.document import Document


class BatchComponent(Document):
	pass
