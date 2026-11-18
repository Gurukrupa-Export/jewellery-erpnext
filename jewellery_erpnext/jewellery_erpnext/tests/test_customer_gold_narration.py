# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Customer Gold narration: what lets accounts tally a settlement JE against its receipts.

On kggk-uat every Customer Gold GL line on Customer Goods Receive - KGJPL said nothing about
its customer, receipt or delivery: receipts and returns read "Accounting Entry for Stock", and
the five settlement JEs had an EMPTY GL remark -- their note sat in the header ``user_remark``,
which erpnext 16 never copies to GL. These tests pin the text, the one-line-per-receipt JE, the
reconcile-before-post order, the Connections links and the backfill.

Pure-logic per the suite convention: no document is created and every DB read is patched.
"""

import unittest
from unittest.mock import MagicMock, call, patch

import frappe

from jewellery_erpnext.customer_subcontracting import customer_gold_dashboards as dash
from jewellery_erpnext.customer_subcontracting import customer_gold_fulfilment as cgf
from jewellery_erpnext.customer_subcontracting import customer_gold_narration as narr
from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	VALUATION_NOMINAL,
)

ALLOC = "jewellery_erpnext.customer_subcontracting.customer_gold_allocations"
LIABILITY = "Customer Gold Liability - CG"
ADJUSTMENT = "Customer Gold COGS Adjustment - CG"
NAMES = {"CUST": "Gurukrupa Export", "CUST-2": "Second Customer"}


def _alloc(customer, voucher, row, batch, gross, fine, amount):
	return frappe._dict(
		customer=customer,
		receipt_voucher=voucher,
		receipt_row=row,
		source_batch=batch,
		gross_qty=gross,
		fine_qty=fine,
		amount=amount,
	)


def _receipt_row(name, idx, item="M-G-24KT", uom="Gram"):
	return frappe._dict(name=name, parent="R", idx=idx, item_code=item, stock_uom=uom)


def _dn(**fields):
	values = {
		"doctype": "Delivery Note",
		"name": "DN-48",
		"company": "CG Co",
		"posting_date": "2026-10-30",
		"items": [frappe._dict(against_sales_order="SO-7", against_sales_invoice=None)],
	}
	values.update(fields)
	return frappe._dict(values)


class _NarrationCase(unittest.TestCase):
	"""Allocations, receipt rows and customer names are answered here; nothing reads the site."""

	allocations = ()
	rows = {}

	def setUp(self):
		real_cached = frappe.get_cached_value

		def cached_value(doctype, *args, **kwargs):
			if doctype == "Customer":
				return NAMES.get(args[0])
			return real_cached(doctype, *args, **kwargs)

		for p in (
			patch.object(
				narr,
				"_settlement_allocations",
				side_effect=lambda names: list(self.allocations),
			),
			patch.object(
				narr, "_receipt_rows", side_effect=lambda names: dict(self.rows)
			),
			patch.object(narr.frappe, "get_cached_value", side_effect=cached_value),
		):
			p.start()
			self.addCleanup(p.stop)


class TestSettlementLines(_NarrationCase):
	"""One liability line per receipt row; per customer they add up to the settled amount."""

	rows = {"R1": _receipt_row("R1", 1), "R2": _receipt_row("R2", 2, item="M-G-22KT")}

	def test_one_receipt_gives_one_line_with_the_whole_breakup(self):
		"""DN-48: 5 g of receipt KGJPL-SE-CGR-26-00057, Rs 72,651.00."""
		self.allocations = [_alloc("CUST", "CGR-57", "R1", "B-50", 5.0, 5.0, 72651.0)]
		(line,) = narr.settlement_lines(_dn(), ["EV-1"], {"CUST": 72651.0}, 2)
		self.assertEqual(line.amount, 72651.0)
		self.assertEqual(line.receipt_voucher, "CGR-57")
		for text in (
			"Customer Gold released",
			"Customer: CUST - Gurukrupa Export",
			"Delivery Note: DN-48",
			"Sales Order: SO-7",
			"Receipt: CGR-57 row 1 (M-G-24KT)",
			"Batch: B-50",
			"Qty: 5.000 Gram",
			"Fine: 5.000 g",
			"Amount: 72,651.00",
		):
			self.assertIn(text, line.remark)

	def test_serials_of_one_receipt_row_are_one_line(self):
		"""Six serials from one receipt row are one line, not six."""
		self.allocations = [
			_alloc("CUST", "CGR-57", "R1", "B-50", 1.0, 1.0, 100.0) for _ in range(6)
		]
		(line,) = narr.settlement_lines(_dn(), ["EV"], {"CUST": 600.0}, 2)
		self.assertEqual(line.amount, 600.0)
		self.assertIn("Qty: 6.000 Gram", line.remark)

	def test_two_receipts_are_two_lines_that_add_up_to_the_total(self):
		self.allocations = [
			_alloc("CUST", "CGR-57", "R1", "B-50", 3.0, 3.0, 300.0),
			_alloc("CUST", "CGR-58", "R2", "B-51", 2.0, 1.83, 183.33),
		]
		lines = narr.settlement_lines(_dn(), ["EV"], {"CUST": 483.33}, 2)
		self.assertEqual([line.receipt_voucher for line in lines], ["CGR-57", "CGR-58"])
		self.assertAlmostEqual(sum(line.amount for line in lines), 483.33, places=2)
		self.assertIn("Receipt: CGR-58 row 2 (M-G-22KT)", lines[1].remark)

	def test_what_no_receipt_explains_is_its_own_labelled_line(self):
		"""A residual reconcile would not absorb stays visible, and the JE still balances."""
		self.allocations = [_alloc("CUST", "CGR-57", "R1", "B-50", 3.0, 3.0, 300.0)]
		lines = narr.settlement_lines(_dn(), ["EV"], {"CUST": 350.0}, 2)
		self.assertEqual([line.amount for line in lines], [300.0, 50.0])
		self.assertIsNone(lines[1].receipt_voucher)
		self.assertIn(narr.UNTRACED, lines[1].remark)
		self.assertIn("Amount: 50.00", lines[1].remark)

	def test_an_over_covered_total_shows_the_difference_with_its_sign(self):
		self.allocations = [_alloc("CUST", "CGR-57", "R1", "B-50", 3.0, 3.0, 300.0)]
		lines = narr.settlement_lines(_dn(), ["EV"], {"CUST": 299.0}, 2)
		self.assertEqual([line.amount for line in lines], [300.0, -1.0])
		self.assertIn("Amount: -1.00", lines[1].remark)

	def test_a_delivery_with_no_recorded_provenance_is_one_untraced_line(self):
		self.allocations = []
		(line,) = narr.settlement_lines(_dn(), ["EV"], {"CUST": 100.0}, 2)
		self.assertEqual(line.amount, 100.0)
		self.assertIn(narr.UNTRACED, line.remark)

	def test_a_customer_whose_amount_rounds_to_zero_gets_no_line(self):
		self.assertEqual(narr.settlement_lines(_dn(), [], {"CUST": 0.001}, 2), [])

	def test_each_customer_gets_their_own_lines(self):
		self.allocations = [
			_alloc("CUST", "CGR-57", "R1", "B-50", 1.0, 1.0, 100.0),
			_alloc("CUST-2", "CGR-58", "R2", "B-51", 1.0, 1.0, 40.0),
		]
		lines = narr.settlement_lines(_dn(), ["EV"], {"CUST": 100.0, "CUST-2": 40.0}, 2)
		self.assertEqual(
			[(line.customer, line.amount) for line in lines],
			[("CUST", 100.0), ("CUST-2", 40.0)],
		)
		self.assertIn("Customer: CUST-2 - Second Customer", lines[1].remark)

	def test_a_sales_return_restores_and_keeps_the_negative_sign(self):
		self.allocations = [_alloc("CUST", "CGR-57", "R1", "B-50", -1.0, -1.0, -100.0)]
		(line,) = narr.settlement_lines(_dn(is_return=1), ["EV"], {"CUST": -100.0}, 2)
		self.assertEqual(line.amount, -100.0)
		self.assertTrue(line.remark.startswith("Customer Gold restored"))
		self.assertIn("Qty: 1.000 Gram", line.remark)
		self.assertIn("Amount: 100.00", line.remark)

	def test_a_sales_invoice_that_delivers_names_its_notes_and_orders(self):
		doc = frappe._dict(
			doctype="Sales Invoice",
			name="SI-42",
			items=[frappe._dict(sales_order="SO-7", delivery_note="DN-45")],
		)
		self.assertEqual(
			narr.document_context(doc),
			"Sales Invoice: SI-42 | Sales Order: SO-7 | Delivery Note: DN-45",
		)

	def test_the_summary_names_the_document_each_customer_and_their_receipts(self):
		self.allocations = [
			_alloc("CUST", "CGR-57", "R1", "B-50", 3.0, 3.0, 300.0),
			_alloc("CUST", "CGR-58", "R2", "B-51", 2.0, 2.0, 200.0),
		]
		lines = narr.settlement_lines(_dn(), ["EV"], {"CUST": 550.0}, 2)
		summary = narr.settlement_summary(_dn(), lines)
		self.assertTrue(
			summary.startswith("Customer Gold settlement | Delivery Note: DN-48")
		)
		self.assertIn("CUST - Gurukrupa Export: 550.00 from CGR-57, CGR-58", summary)


class TestSettlementEntryCarriesTheNarration(_NarrationCase):
	"""The JE that reaches GL: line remarks per receipt, header remark kept by custom_remark."""

	rows = {"R1": _receipt_row("R1", 1), "R2": _receipt_row("R2", 2)}

	def setUp(self):
		super().setUp()
		self.entry = MagicMock()
		new_doc = MagicMock(
			side_effect=lambda doctype, **kwargs: {}
			if doctype == "Journal Entry Account"
			else self.entry
		)
		real_cached = narr.frappe.get_cached_value
		self.account_type = ""
		self.merge = False

		def cached_value(doctype, *args, **kwargs):
			if doctype == "Company" and args[:2] == ("CG Co", "cost_center"):
				return "Main - CG"
			if doctype == "Account":
				return self.account_type
			return real_cached(doctype, *args, **kwargs)

		for p in (
			patch.object(cgf, "validate_settlement_accounts"),
			patch.object(cgf.frappe, "new_doc", new_doc),
			patch.object(cgf.frappe, "get_cached_value", side_effect=cached_value),
			patch.object(
				narr, "merges_similar_account_heads", side_effect=lambda: self.merge
			),
		):
			p.start()
			self.addCleanup(p.stop)

	def _post(self, per_customer, total):
		accounts = frappe._dict(
			liability_account=LIABILITY, cogs_adjustment_account=ADJUSTMENT
		)
		events = [
			frappe._dict(name="EV-1", cg_source_row="X", serial_no="S", batch_no="B")
		]
		cgf._build_settlement_entry(_dn(), accounts, per_customer, total, 2, events)
		return [
			c.args[1]
			for c in self.entry.append.call_args_list
			if c.args[0] == "accounts"
		]

	def test_one_liability_line_per_receipt_each_with_its_own_remark(self):
		self.allocations = [
			_alloc("CUST", "CGR-57", "R1", "B-50", 3.0, 3.0, 300.0),
			_alloc("CUST", "CGR-58", "R2", "B-51", 2.0, 2.0, 200.0),
		]
		lines = self._post({"CUST": 500.0}, 500.0)
		self.assertEqual(
			[line["account"] for line in lines], [LIABILITY, LIABILITY, ADJUSTMENT]
		)
		self.assertEqual(
			[line["debit_in_account_currency"] for line in lines[:2]], [300.0, 200.0]
		)
		self.assertEqual(lines[2]["credit_in_account_currency"], 500.0)
		self.assertIn("Receipt: CGR-57 row 1", lines[0]["user_remark"])
		self.assertIn("Receipt: CGR-58 row 2", lines[1]["user_remark"])
		self.assertNotIn("user_remark", lines[2])

	def test_the_header_remark_is_kept_by_custom_remark(self):
		"""Without custom_remark, create_remarks would rebuild remark and drop the text."""
		self.allocations = [_alloc("CUST", "CGR-57", "R1", "B-50", 3.0, 3.0, 300.0)]
		self._post({"CUST": 300.0}, 300.0)
		self.assertEqual(self.entry.custom_remark, 1)
		self.assertTrue(self.entry.remark.startswith("Customer Gold settlement"))
		self.assertIn("from CGR-57", self.entry.remark)
		# The old note stays where it was, for anyone reading the JE form.
		self.assertIn("Events: EV-1", self.entry.user_remark)

	def test_a_return_credits_every_receipt_line_and_debits_the_adjustment(self):
		self.allocations = [_alloc("CUST", "CGR-57", "R1", "B-50", -1.0, -1.0, -40.0)]
		lines = self._post({"CUST": -40.0}, -40.0)
		self.assertEqual(lines[0]["credit_in_account_currency"], 40.0)
		self.assertEqual(lines[0]["debit_in_account_currency"], 0)
		self.assertEqual(lines[1]["debit_in_account_currency"], 40.0)

	def _gl(self, lines, merge):
		"""The GL rows erpnext writes for these JE lines: ``JournalEntry.build_gl_map`` composes
		each row's remark from the line's ``user_remark`` and the header ``remark``, and
		``make_gl_entries`` runs ``merge_similar_entries`` when the setting is on."""
		from erpnext.accounts.general_ledger import merge_similar_entries

		gl_map = [
			frappe._dict(
				company="CG Co",
				voucher_type="Journal Entry",
				voucher_no="JE-NEW",
				account=line["account"],
				cost_center=line.get("cost_center"),
				party_type=line.get("party_type"),
				party=line.get("party"),
				debit=line["debit_in_account_currency"],
				credit=line["credit_in_account_currency"],
				debit_in_account_currency=line["debit_in_account_currency"],
				credit_in_account_currency=line["credit_in_account_currency"],
				remarks="\n".join(
					r for r in (line.get("user_remark"), self.entry.remark) if r
				),
			)
			for line in lines
		]
		return merge_similar_entries(gl_map, precision=2) if merge else gl_map

	def _two_receipts_two_customers(self):
		self.allocations = [
			_alloc("CUST", "CGR-57", "R1", "B-50", 3.0, 3.0, 300.0),
			_alloc("CUST", "CGR-58", "R2", "B-51", 2.0, 2.0, 200.0),
			_alloc("CUST-2", "CGR-59", "R1", "B-52", 1.0, 1.0, 40.0),
		]
		return {"CUST": 500.0, "CUST-2": 40.0}, 540.0

	def test_with_merge_on_one_liability_line_names_every_receipt(self):
		"""Merge Similar Account Heads is on at kggk-uat and kggk-prod. Lines without a party merge
		across customers too, so all of them become one line whose remark keeps every receipt."""
		self.merge = True
		per_customer, total = self._two_receipts_two_customers()
		lines = self._post(per_customer, total)
		self.assertEqual([line["account"] for line in lines], [LIABILITY, ADJUSTMENT])
		self.assertEqual(lines[0]["debit_in_account_currency"], 540.0)
		self.assertEqual(lines[1]["credit_in_account_currency"], 540.0)
		remark = lines[0]["user_remark"].splitlines()
		self.assertEqual(len(remark), 3)
		for text, receipt in zip(remark, ("CGR-57", "CGR-58", "CGR-59"), strict=True):
			self.assertIn(f"Receipt: {receipt} row", text)

	def test_with_merge_on_and_a_party_account_each_customer_keeps_one_line(self):
		self.merge = True
		self.account_type = "Payable"
		per_customer, total = self._two_receipts_two_customers()
		lines = self._post(per_customer, total)
		liability = [line for line in lines if line["account"] == LIABILITY]
		self.assertEqual(
			[(line["party"], line["debit_in_account_currency"]) for line in liability],
			[("CUST", 500.0), ("CUST-2", 40.0)],
		)
		self.assertIn("CGR-58", liability[0]["user_remark"])
		self.assertNotIn("CGR-59", liability[0]["user_remark"])

	def test_the_gl_names_every_receipt_whatever_the_merge_setting(self):
		"""What reaches GL Entry, as erpnext builds and merges it: no row may carry an amount whose
		receipts its remark does not name -- the P2 on #1676, where the merged row named only the
		first receipt."""
		per_customer, total = self._two_receipts_two_customers()
		for merge in (False, True):
			with self.subTest(merge=merge):
				self.entry.reset_mock()
				self.merge = merge
				rows = [
					row
					for row in self._gl(self._post(per_customer, total), merge)
					if row.account == LIABILITY
				]
				self.assertEqual(len(rows), 1 if merge else 3)
				self.assertAlmostEqual(sum(row.debit for row in rows), 540.0, places=2)
				named = {
					r: sum(f"Receipt: {r} row" in row.remarks for row in rows)
					for r in ("CGR-57", "CGR-58", "CGR-59")
				}
				self.assertEqual(named, {"CGR-57": 1, "CGR-58": 1, "CGR-59": 1})

	def test_unfolded_lines_would_lose_receipts_when_erpnext_merges_them(self):
		"""The failure the folding prevents, shown with erpnext's own merge."""
		per_customer, total = self._two_receipts_two_customers()
		self.merge = False
		(row,) = [
			r
			for r in self._gl(self._post(per_customer, total), True)
			if r.account == LIABILITY
		]
		self.assertEqual(row.debit, 540.0)
		self.assertIn("Receipt: CGR-57 row", row.remarks)
		# The header summary still lists CGR-58, but its line -- its amount, batch and grams -- is gone.
		self.assertNotIn("Receipt: CGR-58 row", row.remarks)


class TestReconcileRunsBeforeTheEntry(unittest.TestCase):
	"""The JE's receipt lines are read from the allocations, so they must already tie."""

	def test_reconcile_then_build_then_stamp(self):
		order = MagicMock()
		real_get_values = frappe.db.get_values
		event = frappe._dict(
			name="EV-1",
			customer="CUST",
			cg_carrying_value_delta=-100.0,
			cg_source_row="R",
			serial_no="S",
			batch_no="B",
		)
		with patch.object(
			cgf, "get_customer_gold_valuation_policy", return_value=VALUATION_NOMINAL
		), patch.object(
			cgf,
			"get_customer_gold_company_settings",
			return_value=frappe._dict(
				liability_account=LIABILITY, cogs_adjustment_account=ADJUSTMENT
			),
		), patch(
			f"{cgf.__name__}.frappe.db.get_values",
			side_effect=lambda doctype, *a, **k: [event]
			if doctype == cgf.LEDGER_DOCTYPE
			else real_get_values(doctype, *a, **k),
		), patch(f"{cgf.__name__}.frappe.db.set_value"), patch(
			f"{cgf.__name__}.frappe.get_precision", return_value=2
		), patch(f"{ALLOC}.reconcile_to_settlement", order.reconcile), patch(
			f"{ALLOC}.stamp_settlement_voucher", order.stamp
		), patch.object(cgf, "_build_settlement_entry", order.build):
			order.build.return_value = "JE-1"
			cgf.settle_customer_gold_liability(_dn(), ["EV-1"])

		self.assertEqual(
			[c[0] for c in order.mock_calls],
			["reconcile", "build", "stamp"],
		)
		self.assertEqual(order.reconcile.call_args, call(["EV-1"], {"CUST": 100.0}, 2))


class TestStockEntryNarration(_NarrationCase):
	rows = {"R1": _receipt_row("R1", 1)}

	def _receipt(self, **fields):
		values = dict(
			doctype="Stock Entry",
			name="CGR-57",
			_customer="CUST",
			custom_gold_rate_per_gram=14530.2,
			custom_gold_rate_date=None,
			items=[
				frappe._dict(
					idx=1,
					item_code="M-G-24KT",
					qty=5,
					transfer_qty=5,
					stock_uom="Gram",
					batch_no="B-50",
					basic_rate=14530.2,
					basic_amount=72651.0,
				)
			],
		)
		values.update(fields)
		return frappe._dict(values)

	def test_a_receipt_names_the_customer_rate_and_each_row(self):
		text = narr.receipt_narration(self._receipt())
		header, row = text.splitlines()
		self.assertEqual(
			header,
			"Customer Gold received | Customer: CUST - Gurukrupa Export | Gold rate: 14,530.20 per g",
		)
		self.assertEqual(
			row,
			"Row 1: M-G-24KT | Qty: 5.000 Gram | Batch: B-50 | Rate: 14,530.20 | Amount: 72,651.00",
		)

	def test_a_zero_value_receipt_shows_no_rate_or_amount(self):
		doc = self._receipt(custom_gold_rate_per_gram=0)
		doc["items"][0].update(basic_rate=0, basic_amount=0)
		self.assertNotIn("Amount", narr.receipt_narration(doc))
		self.assertNotIn("Gold rate", narr.receipt_narration(doc))

	def test_a_return_names_the_receipt_row_it_gives_back(self):
		doc = frappe._dict(
			doctype="Stock Entry",
			name="CGI-30",
			_customer="CUST",
			custom_cg_issue_against="CGR-53",
			items=[
				frappe._dict(
					idx=1,
					item_code="M-G-24KT",
					qty=2,
					transfer_qty=2,
					stock_uom="Gram",
					batch_no="B-47",
					customer="CUST",
					against_stock_entry="CGR-53",
					ste_detail="R1",
					basic_rate=14676.7,
					basic_amount=29353.4,
				),
				frappe._dict(
					idx=2, item_code="X", qty=1, stock_uom="Nos", batch_no=None
				),
			],
		)
		header, linked, unlinked = narr.return_narration(doc).splitlines()
		self.assertEqual(
			header,
			"Customer Gold returned | Customer: CUST - Gurukrupa Export | Receipt: CGR-53",
		)
		self.assertIn("Against receipt: CGR-53 row 1 (M-G-24KT)", linked)
		self.assertIn("Amount: 29,353.40", linked)
		self.assertIn(narr.UNTRACED, unlinked)

	def test_the_users_own_remark_is_kept_and_the_block_replaced_not_repeated(self):
		first = narr.merge_block("Handed over by Ravi", "Customer Gold received | A")
		self.assertEqual(first, "Handed over by Ravi\nCustomer Gold received | A")
		again = narr.merge_block(first, "Customer Gold received | B\nRow 1: X")
		self.assertEqual(
			again, "Handed over by Ravi\nCustomer Gold received | B\nRow 1: X"
		)
		self.assertEqual(
			narr.merge_block(None, "Customer Gold returned | C"),
			"Customer Gold returned | C",
		)

	def test_the_hook_writes_remarks_only_for_customer_gold_entries(self):
		doc = self._receipt(remarks="note")
		with patch(
			"jewellery_erpnext.customer_subcontracting.customer_gold_receipt._receipt_settings",
			return_value=None,
		), patch(
			"jewellery_erpnext.customer_subcontracting.customer_gold_return._applies",
			return_value=False,
		):
			narr.apply_stock_entry_narration(doc)
		self.assertEqual(doc.remarks, "note")

		with patch(
			"jewellery_erpnext.customer_subcontracting.customer_gold_receipt._receipt_settings",
			return_value=frappe._dict(
				customer_goods_stock_entry_type="Customer Goods Received"
			),
		):
			narr.apply_stock_entry_narration(doc)
		self.assertTrue(
			doc.remarks.startswith("note\nCustomer Gold received | Customer: CUST")
		)


class TestBackfill(_NarrationCase):
	"""Text only, live GL lines only, and a second run changes nothing."""

	def test_a_stock_entry_gets_its_block_on_the_document_and_its_gl(self):
		receipt = frappe._dict(
			doctype="Stock Entry",
			name="CGR-57",
			_customer="CUST",
			remarks=None,
			items=[],
		)
		settings = frappe._dict(
			customer_goods_stock_entry_type="Customer Goods Received",
			customer_gold_return_stock_entry_type=None,
		)
		with patch.object(
			narr, "_vouchers_with_events", return_value=["CGR-57"]
		), patch.object(narr.frappe, "get_doc", return_value=receipt), patch.object(
			narr.frappe.db, "set_value"
		) as set_value, patch.object(narr.frappe.db, "sql") as sql:
			self.assertEqual(narr.backfill_stock_entries(settings), ["CGR-57"])

		text = set_value.call_args.args[3]
		self.assertTrue(text.startswith("Customer Gold received | Customer: CUST"))
		self.assertEqual(set_value.call_args.kwargs, {"update_modified": False})
		(gl_update,) = sql.call_args_list
		self.assertIn("is_cancelled = 0", gl_update.args[0])
		self.assertEqual(gl_update.args[1], (text, "Stock Entry", "CGR-57"))

		receipt.remarks = text
		with patch.object(
			narr, "_vouchers_with_events", return_value=["CGR-57"]
		), patch.object(narr.frappe, "get_doc", return_value=receipt), patch.object(
			narr.frappe.db, "set_value"
		) as set_value:
			self.assertEqual(narr.backfill_stock_entries(settings), [])
		set_value.assert_not_called()

	def test_an_old_settlement_is_rebuilt_from_its_own_events(self):
		"""The same events and sign the settlement used; receipt lines under the summary."""
		self.allocations = [_alloc("CUST", "CGR-57", "R1", "B-50", 5.0, 5.0, 72651.0)]
		self.rows = {"R1": _receipt_row("R1", 1)}
		events = [
			frappe._dict(
				name="EV-1",
				customer="CUST",
				reference_doctype="Delivery Note",
				reference_docname="DN-48",
				cg_carrying_value_delta=-72651.0,
			)
		]
		real_get_all = frappe.get_all
		with patch.object(
			narr.frappe,
			"get_all",
			side_effect=lambda doctype, *a, **k: events
			if doctype == narr.LEDGER_DOCTYPE
			else real_get_all(doctype, *a, **k),
		), patch.object(narr.frappe, "get_doc", return_value=_dn()):
			remark = narr.settlement_remark_for("JE-28")
		summary, line = remark.splitlines()
		self.assertIn("CUST - Gurukrupa Export: 72,651.00 from CGR-57", summary)
		self.assertIn("Receipt: CGR-57 row 1", line)


class TestConnections(unittest.TestCase):
	"""The breakup on the standard Connections tab, added beside what erpnext already shows."""

	def test_the_receipt_links_its_allocations_and_keeps_erpnexts_own(self):
		data = dash.stock_entry(
			{
				"fieldname": "stock_entry",
				"non_standard_fieldnames": {
					"Stock Reservation Entry": "from_voucher_no"
				},
				"transactions": [
					{"label": "Stock Reservation", "items": ["Stock Reservation Entry"]}
				],
			}
		)
		self.assertEqual(data.fieldname, "stock_entry")
		self.assertEqual(
			data.non_standard_fieldnames,
			{
				"Stock Reservation Entry": "from_voucher_no",
				"Customer Gold Allocation": "receipt_voucher",
			},
		)
		self.assertEqual(
			[g["items"] for g in data.transactions],
			[["Stock Reservation Entry"], ["Customer Gold Allocation"]],
		)

	def test_each_document_links_by_its_own_field(self):
		for hook, fieldname in (
			(dash.journal_entry, "settlement_voucher"),
			(dash.delivery_note, "reference_docname"),
			(dash.sales_invoice, "reference_docname"),
		):
			with self.subTest(fieldname=fieldname):
				data = hook({})
				self.assertEqual(
					data.non_standard_fieldnames["Customer Gold Allocation"], fieldname
				)

	def test_running_twice_adds_the_group_once(self):
		data = dash.journal_entry(dash.journal_entry({}))
		self.assertEqual(len(data.transactions), 1)
