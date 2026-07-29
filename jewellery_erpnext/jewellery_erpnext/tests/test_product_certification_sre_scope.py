# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Unit tests for the Sales-Order blast radius of Product Certification's SRE consumption.

The bug: ``get_stock_item_against_mwo`` collected SREs from two queries. ``sre_list_1`` is
scoped to the certified PMO's MWOs, but ``sre_list_2`` filtered only on
``voucher_type="Sales Order"`` + ``voucher_no`` + ``item_code``. Every MWO on a Sales Order
shares ``voucher_no``, and the metal/finding item codes are common across the whole order, so
certifying one PMO handed every OTHER MWO's live WIP reservation to
``consume_stock_reservation_entry`` (delivered_qty = reserved_qty, status "Delivered").

Measured on kg-gk before the fix: one PC covering 2 MWOs marked 216 SREs across 115 MWOs
Delivered, which then broke Employee IR Process Loss for all of them
(see test_loss_stock_entry_spent_sre).

Mocked/pure-logic style: the real function is driven with a stubbed ``frappe.db`` so the
FILTERS it issues can be captured, rather than building a PMO/BOM/MOP-balance fixture chain.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.product_certification import (
	product_certification as pc,
)

_PC = "jewellery_erpnext.jewellery_erpnext.doctype.product_certification.product_certification"
_P = "jewellery_erpnext.patches.restore_over_consumed_pc_reservations"

SO = "SAL-ORD-2026-00036"
METAL = "M-G-22KT-91.9-Y"
PMO = "PMO-KGJPL-PE01656-001-0012"
OWN_MWO = "MWO-KGJPL-PE01656-001-12-91.9-Y-01"
SIBLING_MWO = "MWO-KGJPL-NE02477-001-30-91.9-Y-01"  # same SO, different PMO


def _run_certification(sre_cols=("manufacturing_work_order",)):
	"""Drive the real ``get_stock_item_against_mwo`` and return its get_all calls.

	Only the SRE-selection block matters here; the Stock Entry row building downstream is
	fed a single benign MOP balance row and its side effects land on a throwaway fake doc.
	"""
	db = MagicMock()
	db.get_table_columns.return_value = list(sre_cols)
	db.get_value.side_effect = lambda dt, *a, **kw: {
		"Manufacturing Work Order": PMO,
		"Parent Manufacturing Order": SO,
	}.get(dt)
	db.get_all.return_value = []

	balance_row = {
		"item_code": METAL,
		"qty_after_transaction_batch_based": 3.4,
		"batch_no": "BATCH-A",
	}

	se_doc = SimpleNamespace(items=[], append=lambda *a, **kw: None)
	doc = SimpleNamespace(type="Issue", name="CRT-1", company="C")
	row = SimpleNamespace(
		idx=1, manufacturing_work_order=OWN_MWO, parent_manufacturing_order=PMO
	)

	with (
		patch("frappe.db", db),
		patch("frappe.get_all", return_value=[OWN_MWO]),
		patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log."
			"get_current_mop_balance_rows",
			return_value=[balance_row],
		),
		patch("frappe.msgprint"),
		patch("frappe.clear_document_cache"),
		patch("frappe.get_doc"),
		patch("frappe.log_error"),
	):
		try:
			pc.get_stock_item_against_mwo(se_doc, doc, row, "S-WH", "T-WH")
		except Exception:
			# Downstream SE-row building is out of scope; the SRE queries have already run.
			pass

	return [
		call.kwargs.get("filters", call.args[1] if len(call.args) > 1 else None)
		for call in db.get_all.call_args_list
		if call.args and call.args[0] == "Stock Reservation Entry"
	]


class TestProductCertificationSreScope(IntegrationTestCase):
	def test_sre_list_2_excludes_reservations_tagged_to_another_mwo(self):
		"""The regression itself: PMO-A's certification must not reach PMO-B's reservation."""
		filters = _run_certification()
		self.assertEqual(len(filters), 2, f"expected both SRE queries, got {filters}")

		so_scoped = [f for f in filters if f.get("voucher_no") == SO]
		self.assertEqual(len(so_scoped), 1)
		allowed = so_scoped[0].get("manufacturing_work_order")

		self.assertIsNotNone(
			allowed, "sre_list_2 must be scoped by manufacturing_work_order"
		)
		self.assertEqual(allowed, ["in", ["", None]])
		self.assertNotIn(SIBLING_MWO, allowed[1])

	def test_sre_list_1_still_covers_the_certified_pmos_own_mwos(self):
		filters = _run_certification()
		mwo_scoped = [f for f in filters if f.get("voucher_no") is None]
		self.assertEqual(len(mwo_scoped), 1)
		self.assertEqual(mwo_scoped[0]["manufacturing_work_order"], ["in", [OWN_MWO]])

	def test_filter_is_omitted_when_the_custom_column_is_absent(self):
		"""Sites without the custom column keep the old behaviour rather than crashing."""
		filters = _run_certification(sre_cols=())
		for f in filters:
			self.assertNotIn("manufacturing_work_order", f)


class TestRestorePatchSelection(IntegrationTestCase):
	"""The repair patch must only take back reservations outside the certified scope."""

	def test_skips_sres_whose_mwo_was_legitimately_certified(self):
		from jewellery_erpnext.patches import restore_over_consumed_pc_reservations as p

		candidates = [
			MagicMock(manufacturing_work_order="MWO-CERTIFIED"),
			MagicMock(manufacturing_work_order="MWO-VICTIM"),
		]
		db = MagicMock()
		db.has_column.return_value = True
		with (
			patch("frappe.db", db),
			patch(f"{_P}._certified_mwos", return_value={"MWO-CERTIFIED"}),
			patch(f"{_P}._candidate_sres", return_value=candidates),
			patch(f"{_P}._blocked_reason", return_value=None),
			patch(f"{_P}._report") as report,
		):
			p.execute(dry_run=True)

		restored, skipped, dry_run = report.call_args[0]
		self.assertEqual([s.manufacturing_work_order for s in restored], ["MWO-VICTIM"])
		self.assertEqual(skipped, [])
		self.assertTrue(dry_run)

	def test_dry_run_never_writes(self):
		from jewellery_erpnext.patches import restore_over_consumed_pc_reservations as p

		db = MagicMock()
		db.has_column.return_value = True
		with (
			patch("frappe.db", db),
			patch(f"{_P}._certified_mwos", return_value=set()),
			patch(
				f"{_P}._candidate_sres",
				return_value=[MagicMock(manufacturing_work_order="M")],
			),
			patch(f"{_P}._blocked_reason", return_value=None),
			patch(f"{_P}._restore") as restore,
			patch(f"{_P}._refresh_bins") as refresh,
			patch(f"{_P}._report"),
		):
			p.execute(dry_run=True)

		restore.assert_not_called()
		refresh.assert_not_called()

	def test_availability_is_consumed_cumulatively_across_restores(self):
		"""95 reservations of 3.4 g each fit one-at-a-time but not in aggregate."""
		from jewellery_erpnext.patches import restore_over_consumed_pc_reservations as p

		def _sre(name, qty):
			return SimpleNamespace(
				name=name,
				item_code=METAL,
				warehouse="Waxing WO",
				reserved_qty=qty,
				reservation_based_on="Qty",
			)

		claimed_wh, claimed_batch = {}, {}
		with patch(
			"erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry."
			"get_available_qty_to_reserve",
			return_value=5.0,
		):
			first = p._blocked_reason(_sre("A", 3.0), claimed_wh, claimed_batch)
			second = p._blocked_reason(_sre("B", 3.0), claimed_wh, claimed_batch)

		self.assertIsNone(first)
		self.assertIsNotNone(second, "second restore must see the first one's claim")
		self.assertIn("already claimed", second)
