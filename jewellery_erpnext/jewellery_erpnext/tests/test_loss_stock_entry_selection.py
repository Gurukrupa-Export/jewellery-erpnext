# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for batch-aggregate SRE selection in the Employee IR loss flow.

A batch's WIP reservation legitimately spans multiple operation-tagged Stock
Reservation Entries in one warehouse (SRE = physical truth). ``_find_sre`` must
therefore pick the reservation that can COVER the loss — preferring the current
operation's SRE, then the largest — instead of blindly preferring the SRE
tagged with the current operation. ``_validate_sre_qty`` must only raise when no
single SRE can absorb the loss, reporting the batch aggregate.
"""

from types import SimpleNamespace
from unittest.mock import patch

from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import (
	loss_stock_entry,
)

# The bug scenario: the operation-matched (Grinding) SRE holds only 0.010 while
# the sibling Casting SRE holds 5.395, both in "Waxing WO - T". A 0.015 loss
# exceeds the op-matched SRE alone but fits the 5.405 batch aggregate.
_WAREHOUSE = "Waxing WO - T"


def _sre_row(name, reserved_qty, mop, warehouse=_WAREHOUSE):
	return {
		"name": name,
		"warehouse": warehouse,
		"reserved_qty": reserved_qty,
		"available_qty": reserved_qty,
		"voucher_qty": 0,
		"reservation_based_on": "Serial and Batch",
		"has_batch_no": 1,
		"company": "KG GK Jewellers Private Limited",
		"voucher_type": "Sales Order",
		"voucher_no": "SAL-ORD-2026-00001",
		"voucher_detail_no": "soi-1",
		"stock_uom": "Gram",
		"manufacturing_operation": mop,
	}


def _row(**fields):
	defaults = {
		"idx": 1,
		"item_code": "M-G-18KT-75.4-Y",
		"batch_no": "KG2F054-MGL18754Y0-F874H",
		"manufacturing_operation": "MOP-525LX",  # Grinding (current op)
	}
	defaults.update(fields)
	return SimpleNamespace(**defaults)


def _eir(**fields):
	defaults = {"name": "gugsvhhbcc"}
	defaults.update(fields)
	return SimpleNamespace(**defaults)


class TestLossSreSelection(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _find_sre(self, rows, row, qty):
		"""Run _find_sre with frappe.db.sql + frappe.get_doc patched."""

		def fake_get_doc(_doctype, name):
			match = next(r for r in rows if r["name"] == name)
			return SimpleNamespace(
				name=match["name"],
				warehouse=match["warehouse"],
				reserved_qty=match["reserved_qty"],
			)

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry.frappe"
		) as mock_frappe:
			mock_frappe.db.sql.return_value = rows
			mock_frappe.get_doc.side_effect = fake_get_doc
			return loss_stock_entry._find_sre(
				_eir(),
				row,
				"MWO-T-NE01084-007-14-75.4-Y-01",
				"employee_loss_details",
				qty,
			)

	def test_find_sre_picks_covering_sibling_over_tiny_op_match(self):
		# Op-matched SRE (0.010) cannot cover 0.015; the 5.395 sibling can.
		rows = [
			_sre_row("MAT-SRE-2026-88092", 0.010, "MOP-525LX"),
			_sre_row("MAT-SRE-2026-88093", 5.395, "MOP-UG559"),
		]
		sre_doc, candidates = self._find_sre(rows, _row(), 0.015)

		self.assertEqual(sre_doc.name, "MAT-SRE-2026-88093")
		# Candidates confined to one warehouse, op-match ordered first.
		self.assertEqual(
			[c["name"] for c in candidates],
			["MAT-SRE-2026-88092", "MAT-SRE-2026-88093"],
		)
		self.assertEqual({c["warehouse"] for c in candidates}, {_WAREHOUSE})

	def test_find_sre_keeps_op_match_when_it_covers(self):
		# Op-matched SRE (1.0) covers 0.015, so it wins over the larger sibling.
		rows = [
			_sre_row("MAT-SRE-OPMATCH", 1.0, "MOP-525LX"),
			_sre_row("MAT-SRE-BIG", 5.395, "MOP-UG559"),
		]
		sre_doc, _candidates = self._find_sre(rows, _row(), 0.015)
		self.assertEqual(sre_doc.name, "MAT-SRE-OPMATCH")

	def test_find_sre_confines_to_op_match_warehouse(self):
		# A same-batch SRE in another warehouse must be excluded from candidates.
		rows = [
			_sre_row("MAT-SRE-88092", 0.010, "MOP-525LX", warehouse=_WAREHOUSE),
			_sre_row("MAT-SRE-88093", 5.395, "MOP-UG559", warehouse=_WAREHOUSE),
			_sre_row("MAT-SRE-OTHER", 9.0, "MOP-OTHER", warehouse="Some Other WH"),
		]
		sre_doc, candidates = self._find_sre(rows, _row(), 0.015)
		self.assertEqual(sre_doc.name, "MAT-SRE-88093")
		self.assertEqual({c["warehouse"] for c in candidates}, {_WAREHOUSE})

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry._"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry.frappe"
	)
	def test_validate_passes_when_selected_sre_covers(
		self, mock_frappe, mock_underscore
	):
		mock_underscore.side_effect = lambda x: x
		candidates = [
			_sre_row("MAT-SRE-2026-88092", 0.010, "MOP-525LX"),
			_sre_row("MAT-SRE-2026-88093", 5.395, "MOP-UG559"),
		]
		sre_doc = SimpleNamespace(
			name="MAT-SRE-2026-88093", warehouse=_WAREHOUSE, reserved_qty=5.395
		)
		# 0.015 <= 5.395 -> no exception.
		loss_stock_entry._validate_sre_qty(
			_eir(), _row(), sre_doc, candidates, 0.015, "employee_loss_details"
		)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry._"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry.frappe"
	)
	def test_validate_throws_when_no_single_sre_covers(
		self, mock_frappe, mock_underscore
	):
		mock_underscore.side_effect = lambda x: x
		mock_frappe.throw.side_effect = ValidationError
		# 0.010 + 0.008 = 0.018 >= 0.015 in aggregate, but no single SRE covers
		# 0.015 -> must throw (single-covering-SRE policy).
		candidates = [
			_sre_row("A", 0.010, "MOP-525LX"),
			_sre_row("B", 0.008, "MOP-UG559"),
		]
		sre_doc = SimpleNamespace(name="A", warehouse=_WAREHOUSE, reserved_qty=0.010)
		with self.assertRaises(ValidationError):
			loss_stock_entry._validate_sre_qty(
				_eir(), _row(), sre_doc, candidates, 0.015, "employee_loss_details"
			)

	def tearDown(self):
		return super().tearDown()
