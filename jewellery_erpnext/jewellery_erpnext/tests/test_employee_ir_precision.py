# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Verify Float weight fields persist at precision 3.

Checks two layers:

1. **Schema** — each Float weight field on the relevant DocTypes carries
   ``precision: "3"`` so Frappe's grid + DB write rounds the same way.
2. **Runtime** — ``round_employee_ir_weights_to_precision`` rounds the
   in-memory doc before validate, so any code path that reads
   ``child.gross_wt`` after validate gets a 3-decimal value regardless of
   the user's keypad input.
"""

import json
import os
from unittest.mock import MagicMock

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.precision import (
	EIR_OPERATION_WEIGHT_FIELDS,
	LOSS_DETAIL_WEIGHT_FIELDS,
	round_employee_ir_weights_to_precision,
)

# Resolve to the app root so the tests stay path-independent.
_APP_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


def _load_doctype_json(rel_path: str) -> dict:
	with open(os.path.join(_APP_ROOT, rel_path)) as f:
		return json.load(f)


def _float_fields_with_precision(doctype_json: dict) -> dict[str, str | None]:
	return {
		f["fieldname"]: f.get("precision")
		for f in doctype_json["fields"]
		if f.get("fieldtype") == "Float"
	}


class TestSchemaPrecision(FrappeTestCase):
	"""DocType JSON precision attribute is the source of truth at migrate
	time — every covered Float weight field must declare precision=3.
	"""

	def test_employee_ir_operation_weight_fields_precision_3(self):
		j = _load_doctype_json(
			"doctype/employee_ir_operation/employee_ir_operation.json"
		)
		fields = _float_fields_with_precision(j)
		for f in EIR_OPERATION_WEIGHT_FIELDS:
			self.assertEqual(
				fields.get(f),
				"3",
				f"{f} on Employee IR Operation must have precision=3",
			)

	def test_employee_loss_details_precision_3(self):
		j = _load_doctype_json(
			"doctype/employee_loss_details/employee_loss_details.json"
		)
		fields = _float_fields_with_precision(j)
		for f in LOSS_DETAIL_WEIGHT_FIELDS:
			self.assertEqual(fields.get(f), "3", f"{f} must have precision=3")

	def test_manually_book_loss_details_precision_3(self):
		j = _load_doctype_json(
			"doctype/manually_book_loss_details/manually_book_loss_details.json"
		)
		fields = _float_fields_with_precision(j)
		for f in LOSS_DETAIL_WEIGHT_FIELDS:
			self.assertEqual(fields.get(f), "3", f"{f} must have precision=3")

	def test_employee_ir_mop_loss_details_total_precision_3(self):
		j = _load_doctype_json("doctype/employee_ir/employee_ir.json")
		fields = _float_fields_with_precision(j)
		self.assertEqual(fields.get("mop_loss_details_total"), "3")

	def test_manufacturing_operation_weight_fields_precision_3(self):
		j = _load_doctype_json(
			"doctype/manufacturing_operation/manufacturing_operation.json"
		)
		fields = _float_fields_with_precision(j)
		# Subset directly tied to weight bookkeeping; Int / time / pcs
		# fields are intentionally excluded from precision=3.
		for f in (
			"gross_wt",
			"net_wt",
			"finding_wt",
			"diamond_wt",
			"gemstone_wt",
			"diamond_wt_in_gram",
			"gemstone_wt_in_gram",
			"received_gross_wt",
			"received_net_wt",
			"loss_wt",
			"prev_gross_wt",
			"other_wt",
		):
			self.assertEqual(
				fields.get(f),
				"3",
				f"{f} on Manufacturing Operation must have precision=3",
			)


class TestRuntimePrecisionRounder(FrappeTestCase):
	"""``round_employee_ir_weights_to_precision`` rounds in-memory doc
	values before validate. Any field with sub-3-decimal input loses tail
	digits.
	"""

	def _doc_with_op(self, **op_overrides):
		doc = MagicMock()
		op = frappe._dict({f: 0.0 for f in EIR_OPERATION_WEIGHT_FIELDS})
		op.update(op_overrides)
		# child.set must mutate the underlying dict.
		op_mock = MagicMock()
		for k, v in op.items():
			setattr(op_mock, k, v)
		op_mock.set.side_effect = lambda k, v: setattr(op_mock, k, v)
		doc.employee_ir_operations = [op_mock]
		doc.mop_loss_details_total = None
		doc.employee_loss_details = []
		doc.manually_book_loss_details = []
		return doc, op_mock

	def test_employee_ir_operation_gross_wt_rounded_to_3(self):
		doc, op = self._doc_with_op(gross_wt=1.234567)
		round_employee_ir_weights_to_precision(doc)
		self.assertAlmostEqual(op.gross_wt, 1.235, places=6)

	def test_received_gross_wt_rounded_to_3(self):
		doc, op = self._doc_with_op(received_gross_wt=2.111199)
		round_employee_ir_weights_to_precision(doc)
		self.assertAlmostEqual(op.received_gross_wt, 2.111, places=6)

	def test_mop_loss_details_total_rounded_when_present(self):
		doc, _op = self._doc_with_op()
		doc.mop_loss_details_total = 5.6789
		round_employee_ir_weights_to_precision(doc)
		self.assertAlmostEqual(doc.mop_loss_details_total, 5.679, places=6)

	def test_loss_detail_proportionally_loss_rounded(self):
		doc, _op = self._doc_with_op()
		row = MagicMock()
		row.proportionally_loss = 0.123456
		row.net_weight = 0.0
		row.received_gross_weight = 0.0
		row.main_slip_consumption = 0.0
		row.set.side_effect = lambda k, v: setattr(row, k, v)
		doc.manually_book_loss_details = [row]
		round_employee_ir_weights_to_precision(doc)
		self.assertAlmostEqual(row.proportionally_loss, 0.123, places=6)
