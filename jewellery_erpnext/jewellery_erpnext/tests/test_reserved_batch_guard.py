# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Unit tests for ``CustomStockEntry.validate_reserved_batches``.

ERPNext >= v16.29 (backport PR #57169) rewrote ``StockController.validate_reserved_batches``
so that an EMPTY ``own_vouchers`` no longer skips the check -- it now means "exclude nothing".
For this app that inversion is fatal: erpnext restricts ``SRE.voucher_type`` to "Sales Order"
(``stock_reservation_entry.py`` ``allowed_voucher_types``), the manufacturing job is carried
on the custom ``manufacturing_work_order`` / ``manufacturing_operation`` Data fields, and no
manufacturing Stock Entry sets ``work_order`` -- so ``own_vouchers`` is empty for 100% of
batch-consuming Stock Entries here and each one is measured against every reservation on that
batch, in warehouses it never touched. On v16.27/v16.28 the body was unreachable instead.

Mocked/pure-logic style (as in test_product_certification_sre_scope): the real method is
driven against stubbed reservation rows rather than a stock/batch/SRE fixture chain, since
the local site carries no Stock Reservation Entry rows at all.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.stock_entry import (
	CustomStockEntry,
)

_BATCH = "GE2D081-MGL18754Y0-02"
_ITEM = "M-G-18KT-75.4-Y"
_DRAWN_WH = "Model Making WO - GEPL"
_OTHER_WH = "Central WO - GEPL"
_GET_BATCH_QTY = "erpnext.stock.doctype.batch.batch.get_batch_qty"


def _sre_row(name, voucher_no, warehouse=_DRAWN_WH, qty=1.119, delivered_qty=0.0):
	return frappe._dict(
		name=name,
		voucher_type="Sales Order",
		voucher_no=voucher_no,
		item_code=_ITEM,
		warehouse=warehouse,
		batch_no=_BATCH,
		qty=qty,
		delivered_qty=delivered_qty,
	)


def _stock_entry(**fields):
	"""A bare CustomStockEntry — only the guard is exercised, so skip Document.__init__.

	``BaseDocument.get`` falls back to ``self.__dict__`` (base_document.py:338), so plain
	attribute assignment is enough for ``self.get(...)`` to resolve.
	"""
	se = CustomStockEntry.__new__(CustomStockEntry)
	se.doctype = "Stock Entry"
	se.name = "MAT-STE-123282"
	se.posting_date = "2026-07-29"
	se.posting_time = "18:16:14"
	se.work_order = None
	se.subcontracting_inward_order = None
	se.auto_created = 0
	se.manufacturing_order = None
	se.manufacturing_work_order = None
	se.custom_serial_number_creator = None
	se.items = [frappe._dict(item_code=_ITEM, s_warehouse=_DRAWN_WH)]
	for key, value in fields.items():
		setattr(se, key, value)
	return se


def _manufacturing_entry(**fields):
	fields.setdefault("auto_created", 1)
	fields.setdefault("manufacturing_order", "PMO-GEPL-EA02216-001-0238")
	fields.setdefault("manufacturing_work_order", "MWO-GEPL-EA02216-001-238-01")
	fields.setdefault("custom_serial_number_creator", "167v6nfe1g")
	return _stock_entry(**fields)


class TestReservedBatchGuard(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, se, reserved_rows, own_sres=(), batch_qty=0.803):
		"""Drive the real guard; return (thrown, warnings)."""
		warnings = []
		with (
			patch.object(frappe.db, "get_single_value", return_value=1),
			patch.object(frappe, "get_all", return_value=[_BATCH]),
			patch.object(frappe, "get_precision", return_value=3),
			patch.object(
				CustomStockEntry, "_own_reservation_names", return_value=set(own_sres)
			),
			patch.object(
				CustomStockEntry,
				"_get_scoped_reserved_batches",
				return_value=reserved_rows,
			),
			patch(_GET_BATCH_QTY, return_value=batch_qty),
			patch.object(frappe, "log_error"),
			patch.object(
				frappe, "msgprint", side_effect=lambda msg, **kw: warnings.append(msg)
			),
		):
			thrown = None
			try:
				se.validate_reserved_batches()
			except frappe.ValidationError as exc:
				thrown = exc
		return thrown, warnings

	def test_own_job_reservation_is_exempt(self):
		# The job's own SRE is the only claim on the batch: consuming it must not trip
		# the guard. Upstream cannot express this — voucher_no is a shared Sales Order,
		# so the exemption keys on SRE name instead.
		se = _manufacturing_entry()
		rows = [_sre_row("55ivo6jenr", "SAL-ORD-2026-02406", qty=1.009)]
		thrown, warnings = self._run(se, rows, own_sres={"55ivo6jenr"})
		self.assertIsNone(thrown)
		self.assertEqual(warnings, [])

	def test_reservation_in_untouched_warehouse_is_ignored(self):
		# Upstream has no warehouse scoping: a reservation on the same batch in a
		# warehouse this entry never drew from would block it.
		se = _manufacturing_entry()
		rows = [_sre_row("other1", "SAL-ORD-2026-00726", warehouse=_OTHER_WH)]
		thrown, warnings = self._run(se, rows)
		self.assertIsNone(thrown)
		self.assertEqual(warnings, [])

	def test_fully_delivered_reservation_is_ignored(self):
		se = _manufacturing_entry()
		rows = [_sre_row("spent", "SAL-ORD-2026-00726", qty=1.119, delivered_qty=1.119)]
		thrown, warnings = self._run(se, rows)
		self.assertIsNone(thrown)
		self.assertEqual(warnings, [])

	def test_manufacturing_movement_warns_instead_of_throwing(self):
		# The reproduction of SNC 167v6nfe1g: three foreign Sales Orders hold 1.119 in the
		# warehouse this entry drew from, and only 0.803 remains.
		se = _manufacturing_entry()
		rows = [
			_sre_row("f1", "SAL-ORD-2026-00726", qty=0.025),
			_sre_row("f2", "SAL-ORD-2026-00012", qty=0.360),
			_sre_row("f3", "SAL-ORD-2026-00625", qty=0.734),
		]
		thrown, warnings = self._run(se, rows, batch_qty=0.803)
		self.assertIsNone(
			thrown, "internal manufacturing movements must not be blocked"
		)
		self.assertEqual(len(warnings), 1)
		for voucher in (
			"SAL-ORD-2026-00726",
			"SAL-ORD-2026-00012",
			"SAL-ORD-2026-00625",
		):
			self.assertIn(voucher, warnings[0])
		self.assertIn(_BATCH, warnings[0])

	def test_shortfall_never_throws(self):
		# A plain entry carrying no manufacturing marker at all must still be let through.
		# Eight Stock Entry builders in this app stamp no header marker (MR reserve, both
		# finding_mwo legs, main_slip:436, repack:296, batch_rename:394, job_card:180, the
		# get_mapped_doc templates), so any gate would have hard-thrown for them.
		se = _stock_entry()
		rows = [_sre_row("f1", "SAL-ORD-2026-00726", qty=1.119)]
		thrown, warnings = self._run(se, rows, batch_qty=0.803)
		self.assertIsNone(thrown)
		self.assertEqual(len(warnings), 1)

	def test_refining_transfer_warns_only_for_touched_warehouse(self):
		# Refining Entry RFN-MWO-26-00118 -> KGJPL-SE-MT-26-00336: auto_created with a
		# custom_refining_entry link and NO manufacturing_* fields. erpnext bucketed three
		# (batch, warehouse) pairs, one of them 'Tagging Transit' which the entry never drew
		# from; scoping must drop it so the log names only the warehouse actually touched.
		se = _stock_entry(
			auto_created=1,
			custom_refining_entry="RFN-MWO-26-00118",
			items=[frappe._dict(item_code=_ITEM, s_warehouse=_DRAWN_WH)],
		)
		rows = [
			_sre_row("mm1", "SAL-ORD-2026-02260", qty=0.606),
			_sre_row(
				"tt1",
				"SAL-ORD-2026-02251",
				warehouse="Tagging Transit - KGJPL",
				qty=0.038,
			),
		]
		thrown, warnings = self._run(se, rows, batch_qty=0.424)
		self.assertIsNone(thrown)
		self.assertEqual(len(warnings), 1)
		self.assertIn(_DRAWN_WH, warnings[0])
		self.assertNotIn("Tagging Transit", warnings[0])

	def test_sufficient_batch_qty_passes(self):
		se = _stock_entry()
		rows = [_sre_row("f1", "SAL-ORD-2026-00726", qty=1.119)]
		thrown, warnings = self._run(se, rows, batch_qty=2.500)
		self.assertIsNone(thrown)
		self.assertEqual(warnings, [])

	def test_reservation_disabled_short_circuits(self):
		se = _stock_entry()
		with (
			patch.object(frappe.db, "get_single_value", return_value=0),
			patch.object(CustomStockEntry, "_get_scoped_reserved_batches") as scoped,
		):
			se.validate_reserved_batches()
		scoped.assert_not_called()

	def test_entry_with_no_source_warehouse_short_circuits(self):
		# A pure receipt consumes nothing outward; nothing to police.
		se = _stock_entry(items=[frappe._dict(item_code=_ITEM, s_warehouse=None)])
		with (
			patch.object(frappe.db, "get_single_value", return_value=1),
			patch.object(frappe, "get_all", return_value=[_BATCH]),
			patch.object(CustomStockEntry, "_get_scoped_reserved_batches") as scoped,
		):
			se.validate_reserved_batches()
		scoped.assert_not_called()
