# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""The Employee IR receive path that failed on EMP-IR-Labh-2026-14111, in memory.

``create_loss_stock_entries`` builds the real combined Process Loss entry from the IR's
loss row (``_build_combined_loss_se`` is not mocked), and that entry's submit runs the
real ``batch_rename.create_child_batches`` against a stateful batch table seeded with
the incident's full ``...-Y-A`` pool. Only the per-row resolution and the SRE
reduction are stubbed, with the values captured in the failed job's traceback.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting import batch_rename
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir import (
	employee_ir as eir_module,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import (
	loss_stock_entry,
)
from jewellery_erpnext.jewellery_erpnext.tests import (
	test_create_child_batches_namespace as namespace,
)
from jewellery_erpnext.jewellery_erpnext.tests import (
	test_employee_ir_loss_baseline as baseline,
)

PARENT_12_A = "GJCU0009-2F09-M-G-22KT-91.75-Y-12-A"
LOSS_SE = "MAT-STE-19778"


class _FakeStockEntry:
	"""What ``_build_combined_loss_se`` fills in; ``submit`` runs the real child-batch hook."""

	def __init__(self, table):
		self.doctype = "Stock Entry"
		self.name = None
		self.items = []
		self.flags = frappe._dict()
		self._table = table
		self.submitted = False

	def append(self, fieldname, values):
		assert fieldname == "items"
		row = frappe._dict(values)
		row.idx = len(self.items) + 1
		row.name = f"row-{row.idx}"
		self.items.append(row)
		return row

	def insert(self):
		self.name = LOSS_SE
		return self

	def submit(self):
		with (
			patch("frappe.db.sql", side_effect=self._table.sql),
			patch("frappe.db.exists", side_effect=self._table.exists),
		):
			batch_rename.create_child_batches(self)
		self.submitted = True


def _incident():
	"""The IR, its one loss row and the pending entry, as in queue a7f59stcst."""
	loss_row = frappe._dict(
		name="f75khujda1",
		item_code="M-G-22KT-91.75-Y",
		batch_no=PARENT_12_A,
		stock_uom="Gram",
		pcs=None,
		manufacturing_operation="MOP-2609-T8MUIU",
		proportionally_loss=0.01,
	)
	eir = SimpleNamespace(
		name="EMP-IR-Labh-2026-14111",
		company="KG GK Jewellers Private Limited",
		department="Waxing - KGJPL",
		subcontracting="No",
		employee="KGJPL - 00435",
		employee_loss_details=[loss_row],
		manually_book_loss_details=[],
	)
	pending = {
		"row": loss_row,
		"table_name": "employee_loss_details",
		"qty": 0.01,
		"mwo": "MWO-KGJPL-RI00650-006-17-91.75-Y-01",
		"sre_doc": SimpleNamespace(
			name="4slhst9qvt", warehouse="Casting WIP WH 1 - KGJPL"
		),
		"needs_sre_reduction": True,
		"s_warehouse": "Casting WIP WH 1 - KGJPL",
		"t_warehouse": "Waxing Scrap - KGJPL",
		"loss_item": "ML-G-22KT-91.75-Y",
		"inventory_type": "Customer Goods",
		"customer": "GJCU0009",
	}
	return eir, pending


class TestReceiveProcessLossSubmitPath(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _submit_loss(self, table, existing_loss_entry=None):
		eir, pending = _incident()
		entries = []

		def _new_doc(doctype):
			if doctype == "Stock Entry":
				entries.append(_FakeStockEntry(table))
				return entries[-1]
			return namespace._FakeBatch(table)

		reduce_sre = MagicMock()
		with (
			patch.object(
				loss_stock_entry, "_prepare_loss_row", side_effect=lambda *a: pending
			),
			patch.object(loss_stock_entry, "_reduce_sre", reduce_sre),
			patch.object(loss_stock_entry, "_stamp_loss_tree"),
			patch("frappe.db.exists", return_value=existing_loss_entry),
			patch("frappe.db.get_value", return_value=None),
			patch("frappe.new_doc", side_effect=_new_doc),
		):
			loss_stock_entry.create_loss_stock_entries(eir)
		return entries, reduce_sre

	def test_the_incident_receive_now_mints_under_the_parents_own_path(self):
		table = namespace._BatchTable(
			{
				PARENT_12_A,
				*(f"{namespace.COLLAPSED_POOL}-{chr(c)}" for c in range(65, 91)),
			}
		)
		entries, reduce_sre = self._submit_loss(table)

		self.assertEqual(len(entries), 1)
		se = entries[0]
		self.assertTrue(se.submitted)
		self.assertEqual(se.stock_entry_type, "Process Loss")
		self.assertIsNone(getattr(se, "_customer", None))

		consume, produce = se.items
		self.assertEqual(
			(consume.batch_no, consume.customer), (PARENT_12_A, "GJCU0009")
		)
		self.assertEqual(produce.item_code, "ML-G-22KT-91.75-Y")
		self.assertEqual(produce.customer, "GJCU0009")
		self.assertEqual(produce.inventory_type, "Customer Goods")
		self.assertEqual(produce.batch_no, "GJCU0009-2F09-ML-G-22KT-91.75-Y-12-A-A")

		minted = table.minted[0]
		self.assertEqual(minted.reference_name, LOSS_SE)
		self.assertEqual(minted.custom_voucher_detail_no, produce.name)
		self.assertEqual(minted.custom_customer, "GJCU0009")
		reduce_sre.assert_called_once()

	def test_an_existing_loss_entry_is_not_posted_twice(self):
		entries, reduce_sre = self._submit_loss(
			namespace._BatchTable({PARENT_12_A}), existing_loss_entry="MAT-STE-X"
		)
		self.assertEqual(entries, [])
		reduce_sre.assert_not_called()

	def test_the_incident_weights_book_exactly_0_010_g_on_the_customer_batch(self):
		rows = baseline.TestBookMetalLossWaterfall._rows((PARENT_12_A, 12.36))
		ownership = baseline.TestBookMetalLossWaterfall._own(
			**{PARENT_12_A: "Customer Goods"}
		)
		doc, by_batch = baseline.TestBookMetalLossWaterfall._run(
			self, rows, gwt=12.36, r_gwt=12.35, ownership=ownership
		)
		self.assertAlmostEqual(
			by_batch[PARENT_12_A]["proportionally_loss"], 0.01, places=3
		)
		self.assertEqual(doc.spills, [(PARENT_12_A, 0.01)])


class TestPostedCustomerLossQuery(IntegrationTestCase):
	"""The announcement's query runs against the real schema, not a stub."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_the_query_runs_on_this_site(self):
		self.assertEqual(
			list(eir_module._posted_customer_loss_rows("EMP-IR-DOES-NOT-EXIST")), []
		)
