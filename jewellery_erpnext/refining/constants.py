# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Refining vocabulary — the stored Select values, in one place.

These strings are DATA: they live in ``tabRefining Entry.refining_type``,
``tabRefining Material Line.source_type`` and ``tabBatch.custom_batch_type``, so
changing one means migrating rows (see
``patches/rename_refining_scrap_terminology_data``). They are collected here so the
value MAPS that key off them (the naming series map, the external pricing categories,
the batch-type marker) cannot drift out of step with the DocField options.

Deliberately NOT used for the plain ``self.refining_type == "..."`` comparisons or the
``depends_on`` evals in ``refining_entry.json`` — those are eye-verifiable literals, and
a ``depends_on`` string cannot reference a constant at all. ``refining_entry.json``
remains the canonical option list; ``test_refining_entry`` asserts every copy matches it.

This module imports nothing from the app, so ``manufacturing_operation.py`` (which is
outside ``refining/``) can import it without creating a cycle.

History: "Dust Refining" was renamed to "Scrap Refining" and the old "Scrap Refining"
to "Unused/Loose Material Refining" — the business calls the sweep material scrap, and
what used to be called scrap is unused/loose material returned from production. The old
names survive only in the ``RFN-DST-``/``RFN-SCP-`` naming-series prefixes of documents
created before the rename (document names are immutable).
"""

REFINING_TYPE_SCRAP = "Scrap Refining"  # was "Dust Refining" (series RFN-DST-)
REFINING_TYPE_WORK_ORDER = "Work Order Refining"
REFINING_TYPE_SERIAL = "Serial Number Refining"
REFINING_TYPE_UNUSED = (
	"Unused/Loose Material Refining"  # was "Scrap Refining" (RFN-SCP-)
)

#: Dropdown order — must match ``Refining Entry.refining_type`` options.
REFINING_TYPES = (
	REFINING_TYPE_SCRAP,
	REFINING_TYPE_WORK_ORDER,
	REFINING_TYPE_SERIAL,
	REFINING_TYPE_UNUSED,
)

#: ``Refining Entry.refining_type`` options verbatim (leading blank = the empty option).
REFINING_TYPE_OPTIONS = "\n" + "\n".join(REFINING_TYPES)

# Refining Material Line.source_type — where a material row came from.
SOURCE_TYPE_MWO = "MWO"
SOURCE_TYPE_SERIAL = "Serial Number"
SOURCE_TYPE_SCRAP = (
	"Scrap"  # was "Dust" — department sweep fetched from the scrap warehouse
)
SOURCE_TYPE_UNUSED = "Unused/Loose Material"  # was "Scrap" — returned from production
SOURCE_TYPE_LOSS_ITEM = "Loss Item"
SOURCE_TYPE_CONSUMABLE = "Consumable"
SOURCE_TYPE_BOM_COMPONENT = "BOM Component"

#: ``Batch.custom_batch_type`` marker stamped on material returned from production
#: (the "Receive Unused/Loose Material" Manufacturing Operation action). Was "Scrap".
BATCH_TYPE_UNUSED = "Unused/Loose Material"
