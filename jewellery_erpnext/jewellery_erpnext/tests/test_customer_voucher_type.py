# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Customer Voucher Type never leaves the Batch holding an illegal Select value.

``Batch.custom_customer_voucher_type`` is a Select with three options, and it is filled
by COPYING from data this app does not own (the Stock Entry header, an upstream Batch).
A site carrying the literal ``''`` in that chain aborted every Serial Number Creator and
Metal Conversion submit, because ``update_parent_batch_id`` re-saves the produced batch
on each one and frappe then rejects the value it was just handed.

DB-free per the suite convention: ``setUpClass`` is neutralized and the logic runs
against ``SimpleNamespace`` docs with ``frappe.db`` mocked.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.customization.batch.doc_events import (
	utils as batch_utils,
)

# The exact value a real site was carrying: two apostrophes, not an empty string.
QUOTED_EMPTY = "''"


def _batch(**fields):
	defaults = {
		"item": "M-G-22KT-91.75-Y",
		"reference_doctype": "Stock Entry",
		"reference_name": "MAT-STE-17829",
		"custom_voucher_detail_no": "ROW-1",
		"custom_metal_rate": 0,
		"custom_alloy_rate": 0,
		"custom_customer": "GJCU0009",
		"custom_customer_voucher_type": None,
		"custom_inventory_type": None,
		"custom_employee": None,
		"batch_qty": 0,
		"name": "GJCU0009-2F09-RI00210-004-04-A",
	}
	defaults.update(fields)
	return SimpleNamespace(**defaults)


def _db(values, source_batches=()):
	"""A frappe.db stand-in for one ``update_inventory_dimentions`` run.

	``get_all`` answers the three lookups the function makes: the alloy Item Group /
	Item lists (empty), the reference doctype's Table fields (one child table), and
	the consumed rows ``_source_batch_voucher_type`` scans (``source_batches``).
	"""
	db = MagicMock()
	db.has_column.return_value = True
	db.exists.return_value = True

	def get_all(doctype, filters=None, fields=None, **kw):
		if doctype == "DocField":
			return [SimpleNamespace(options=values["__child_doctype__"])]
		if doctype == "Stock Entry Detail":
			return list(source_batches)
		return []

	db.get_all.side_effect = get_all
	db.get_value.side_effect = (
		lambda doctype, name=None, fieldname=None, **kw: values.get(
			(doctype, fieldname)
		)
	)
	return db


def _values(overrides=None):
	values = {
		"__child_doctype__": "Stock Entry Detail",
		("Stock Entry Detail", "inventory_type"): "Customer Goods",
		("Stock Entry Detail", "customer"): "GJCU0009",
		("Stock Entry Detail", "employee"): None,
		("Stock Entry Detail", "basic_rate"): 0,
		("Item", "custom_inventory_type_can_be_customer_goods"): 1,
		("Stock Entry", "customer_voucher_type"): None,
	}
	values.update(overrides or {})
	return values


class TestValidVoucherType(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_the_three_options_pass_through(self):
		for option in batch_utils.CUSTOMER_VOUCHER_TYPES:
			self.assertEqual(batch_utils._valid_voucher_type(option), option)

	def test_quoted_empty_string_is_rejected(self):
		self.assertIsNone(batch_utils._valid_voucher_type(QUOTED_EMPTY))

	def test_empty_and_unknown_values_are_rejected(self):
		for value in (None, "", "   ", "Customer Subcontractin", "0"):
			self.assertIsNone(batch_utils._valid_voucher_type(value))

	def test_surrounding_whitespace_is_tolerated(self):
		self.assertEqual(
			batch_utils._valid_voucher_type("  Customer Repair  "), "Customer Repair"
		)


class TestVoucherTypeStamping(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, batch, values, source_batches=()):
		with patch.object(batch_utils.frappe, "db", _db(values, source_batches)):
			batch_utils.update_inventory_dimentions(batch)
		return batch.custom_customer_voucher_type

	def test_illegal_stock_entry_header_value_is_not_copied(self):
		batch = _batch()
		values = _values({("Stock Entry", "customer_voucher_type"): QUOTED_EMPTY})

		self.assertIsNone(self._run(batch, values))

	def test_legal_stock_entry_header_value_still_wins(self):
		batch = _batch()
		values = _values(
			{("Stock Entry", "customer_voucher_type"): "Customer Subcontracting"}
		)

		self.assertEqual(self._run(batch, values), "Customer Subcontracting")

	def test_illegal_value_already_on_the_batch_is_cleared(self):
		"""The case that broke the submits: the junk is on the row being saved.

		Nothing to copy from, so the pre-existing value would otherwise survive into
		``_validate_selects`` -- which is what aborted an unrelated Manufacture submit
		re-saving this batch for provenance.
		"""
		batch = _batch(
			reference_doctype=None, custom_customer_voucher_type=QUOTED_EMPTY
		)
		values = _values()

		self.assertIsNone(self._run(batch, values))

	def test_illegal_source_batch_value_does_not_block_a_valid_one(self):
		batch = _batch()
		values = _values()
		db = _db(values, source_batches=["POISONED-BATCH", "GOOD-BATCH"])

		# First source batch answers with junk, second with a real option.
		answers = iter([QUOTED_EMPTY, "Customer Repair"])
		lookups = {("Batch", "custom_customer_voucher_type"): lambda: next(answers)}

		def get_value(doctype, name=None, fieldname=None, **kw):
			supplier = lookups.get((doctype, fieldname))
			return supplier() if supplier else values.get((doctype, fieldname))

		db.get_value.side_effect = get_value

		with patch.object(batch_utils.frappe, "db", db):
			batch_utils.update_inventory_dimentions(batch)

		self.assertEqual(batch.custom_customer_voucher_type, "Customer Repair")
