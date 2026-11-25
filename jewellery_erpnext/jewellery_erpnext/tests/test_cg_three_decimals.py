# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Customer Goods flow: weights, purities, rates and amounts are kept and shown at 3 decimals.

kggk-uat showed Customer Gold Ledger quantities at 6 decimals, allocation weights rounded at
6, Metal Conversion percentages at 2, rates at 2 or 4, and messages printing raw floats
("0.137763", "1.5"). These tests pin 3 decimals everywhere the flow stores or shows a number.

Pure-logic per the suite convention: no document is created and every DB read is patched.
"""

import json
import os
import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils.number_format import NumberFormat

from jewellery_erpnext.customer_subcontracting import cg_format, hybrid_findings
from jewellery_erpnext.customer_subcontracting import customer_gold_allocations as alloc
from jewellery_erpnext.customer_subcontracting import customer_gold_fulfilment as cgf
from jewellery_erpnext.customer_subcontracting import customer_gold_narration as narr
from jewellery_erpnext.customer_subcontracting import customer_gold_revaluation as cgv
from jewellery_erpnext.customer_subcontracting.cg_format import fmt3
from jewellery_erpnext.customer_subcontracting.report.customer_gold_traceability import (
	customer_gold_traceability as report,
)
from jewellery_erpnext.customer_subcontracting.sub_utils import gold_usage, snc

APP = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: kg-gk's System Settings number format: Indian grouping, "12,34,567.891".
INDIAN = "#,##,###.##"


def _number_format(string=INDIAN):
	"""Pin the number format ``fmt3`` reads, so a test does not depend on the site's."""
	return patch.object(
		cg_format, "get_number_format", return_value=NumberFormat.from_string(string)
	)


def _fields(relpath, names):
	with open(os.path.join(APP, relpath)) as f:
		meta = json.load(f)
	return {
		d["fieldname"]: d.get("precision")
		for d in meta["fields"]
		if d["fieldname"] in names
	}


class TestFmt3(unittest.TestCase):
	def test_pads_and_rounds_to_three(self):
		with _number_format():
			self.assertEqual(fmt3(1.5), "1.500")
			self.assertEqual(fmt3(2), "2.000")
			self.assertEqual(fmt3(0.137763), "0.138")
			self.assertEqual(fmt3(-1.2345), "-1.234")
			self.assertEqual(fmt3(None), "0.000")
			self.assertEqual(fmt3("4.3"), "4.300")

	def test_groups_like_a_frappe_form(self):
		"""``fmt_money`` alone groups but keeps 2 decimals for "12924.4": "12,924.40"."""
		with _number_format():
			self.assertEqual(fmt3(12924.4), "12,924.400")
			self.assertEqual(fmt3(1234567.891), "12,34,567.891")
			self.assertEqual(fmt3(72651), "72,651.000")
			self.assertEqual(fmt3(-1234.5), "-1,234.500")
		with _number_format("#,###.##"):
			self.assertEqual(fmt3(1234567.891), "1,234,567.891")

	def test_uses_the_sites_decimal_separator(self):
		with _number_format("#.###,##"):
			self.assertEqual(fmt3(12924.4), "12.924,400")
			self.assertEqual(fmt3(1.5), "1,500")

	def test_a_format_without_decimals_still_shows_three(self):
		# "#,###" has no decimal separator, so fmt_money prints 1.5 as "1".
		with _number_format("#,###"):
			self.assertEqual(fmt3(1.5), "1.500")
			self.assertEqual(fmt3(12924.4), "12924.400")

	def test_never_minus_zero(self):
		with _number_format():
			self.assertEqual(fmt3(-0.0004), "0.000")
			self.assertEqual(fmt3(-0.0), "0.000")

	def test_a_format_that_cannot_be_read_does_not_lose_the_message(self):
		# fmt3 mostly builds refusals: the refusal is shown in Frappe's default format rather
		# than replaced by the lookup's own error.
		with patch.object(
			cg_format, "get_number_format", side_effect=RuntimeError("no database")
		):
			self.assertEqual(fmt3(1234.5), "1,234.500")


class TestStoredPrecision(unittest.TestCase):
	def test_ledger_entry_fields_are_three(self):
		names = {
			"cg_gross_qty_delta",
			"cg_fine_gold_delta",
			"cg_reference_qty_delta",
			"cg_reference_purity",
			"cg_carrying_value_delta",
		}
		got = _fields(
			"customer_subcontracting/doctype/customer_gold_ledger_entry/customer_gold_ledger_entry.json",
			names,
		)
		self.assertEqual(got, dict.fromkeys(names, "3"))

	def test_allocation_fields_and_rounding_are_three(self):
		names = {"gross_qty", "fine_qty", "amount"}
		got = _fields(
			"customer_subcontracting/doctype/customer_gold_allocation/customer_gold_allocation.json",
			names,
		)
		self.assertEqual(got, dict.fromkeys(names, "3"))
		self.assertEqual((alloc.QTY_PRECISION, alloc.AMOUNT_PRECISION), (3, 3))

	def test_metal_conversion_percentage_is_three(self):
		got = _fields(
			"jewellery_erpnext/doctype/metal_conversions/metal_conversions.json",
			{"percentage"},
		)
		self.assertEqual(got, {"percentage": "3"})

	def test_gold_rate_custom_fields_are_three(self):
		from jewellery_erpnext.patches import (
			add_customer_gold_rate_check_fields,
			add_customer_gold_rate_snapshot_fields,
		)

		self.assertEqual(add_customer_gold_rate_snapshot_fields.RATE_PRECISION, "3")
		self.assertEqual(add_customer_gold_rate_check_fields.RATE_PRECISION, "3")


class TestSubcontractingLogSettlement(unittest.TestCase):
	def test_settled_and_balance_round_to_three(self):
		log = frappe._dict(
			pending_pure_qty=1.835, settled_pure_qty=0.1, save=MagicMock()
		)
		with patch.object(gold_usage.frappe, "get_doc", return_value=log):
			gold_usage.update_pending_settlement("SCL-1", 0.2000004, "REPACK-1", "B1")
		self.assertEqual(log.settled_pure_qty, 0.3)
		self.assertEqual(log.balance_pure_qty, 1.535)
		self.assertEqual(log.settlement_status, "Partially Settled")

	def test_float_dust_settles_the_log(self):
		# 0.1 + 0.2 is 0.30000000000000004 in floats; unrounded, the log stored that dust
		# (settled 0.30000000000000004, balance -5.5e-17) instead of 0.300 and 0.000.
		log = frappe._dict(pending_pure_qty=0.3, settled_pure_qty=0.1, save=MagicMock())
		with patch.object(gold_usage.frappe, "get_doc", return_value=log):
			gold_usage.update_pending_settlement("SCL-1", 0.2, "REPACK-1", "B1")
		self.assertEqual((log.settled_pure_qty, log.balance_pure_qty), (0.3, 0.0))
		self.assertEqual(log.settlement_status, "Settled")


class TestNarrationMoney(unittest.TestCase):
	"""The GL narration's amounts and rates: grouped, and always at 3 decimals."""

	def test_money_is_padded_and_grouped(self):
		with _number_format():
			self.assertEqual(narr._money(72651), "72,651.000")
			self.assertEqual(narr._money(14530.2), "14,530.200")
			self.assertEqual(narr._money(1234567.891), "12,34,567.891")
			self.assertEqual(narr._money(0.0004), "0.000")

	def test_the_sign_shows_only_against_the_settled_total(self):
		with _number_format():
			self.assertEqual(narr._money(-1, direction=299), "-1.000")
			self.assertEqual(narr._money(-100, direction=-100), "100.000")
			self.assertEqual(narr._money(-0.0004, direction=100), "0.000")

	def test_quantities_are_grouped_too(self):
		with _number_format():
			self.assertEqual(narr._qty(1234.5, "Gram"), "1,234.500 Gram")
			self.assertEqual(narr._qty(5), "5.000 g")


class TestSerialReturnRestoresTheDeliveryExactly(unittest.TestCase):
	"""A serial return restores what its delivery released. The delivery's share is priced at 3
	decimals (``_share_of_components``); restoring it at 2 left the third decimal open."""

	def _original(self, value):
		return frappe._dict(
			name="CGLE-DLV-1",
			cg_gross_qty_delta=-1.0,
			cg_fine_gold_delta=-0.916,
			cg_fine_measurement_status=cgf.STATUS_KNOWN,
			cg_carrying_value_delta=value,
		)

	def test_a_full_return_nets_the_delivery_to_zero(self):
		share = cgf._pin_to_original_value(
			None, self._original(-1234.567), returned_qty=1.0, nominal=True
		)
		self.assertEqual(share.value, 1234.567)
		self.assertEqual(share.fine, 0.916)
		self.assertEqual(-1234.567 + share.value, 0.0)

	def test_a_part_return_is_rounded_at_three(self):
		share = cgf._pin_to_original_value(
			None, self._original(-1234.567), returned_qty=0.25, nominal=True
		)
		# 1234.567 x 0.25 = 308.64175 -> 308.642, not 308.64.
		self.assertEqual(share.value, 308.642)


class TestAllocationQuantitiesTieToTheEvent(unittest.TestCase):
	"""Three thirds of a 1 g delivery: each part rounds to 0.333 g, and the rows used to sum to
	0.999 g of a 1.000 g event. The residual lands on the largest part, as amounts already did."""

	def _written(self, parts, total_amount):
		rows = []
		event = frappe._dict(
			name="CGLE-DLV-1",
			company="CG Co",
			customer="CUST",
			reference_doctype="Delivery Note",
			reference_docname="DN-1",
		)
		with patch.object(
			alloc, "_insert", side_effect=lambda values: rows.append(values)
		), patch.object(alloc, "get_purity_percentage", return_value=91.6):
			alloc.allocate_event(
				event,
				parts,
				alloc.DISPOSITION_FG_DELIVERY,
				alloc.BASIS_COMPONENT,
				"INR",
				total_amount=total_amount,
			)
		return rows

	@staticmethod
	def _receipt(n):
		return frappe._dict(
			name=f"CGLE-R{n}",
			reference_docname=f"CGR-{n}",
			cg_source_row=f"row-{n}",
			batch_no=f"B-{n}",
			item_code="M-G-22KT-91.6-Y",
		)

	def test_the_rows_sum_to_the_event_exactly(self):
		parts = [(self._receipt(n), 1.0 / 3, 100.0 / 3) for n in (1, 2, 3)]
		rows = self._written(parts, total_amount=100.0)

		self.assertEqual([r["gross_qty"] for r in rows], [0.334, 0.333, 0.333])
		self.assertEqual(round(sum(r["gross_qty"] for r in rows), 9), 1.0)
		# 0.916 g fine: 0.30533 each rounds to 0.305, 0.915 in all.
		self.assertEqual([r["fine_qty"] for r in rows], [0.306, 0.305, 0.305])
		self.assertEqual(round(sum(r["fine_qty"] for r in rows), 9), 0.916)
		self.assertEqual([r["amount"] for r in rows], [33.334, 33.333, 33.333])

	def test_parts_that_already_tie_are_left_as_they_are(self):
		parts = [(self._receipt(1), 0.6, 60.0), (self._receipt(2), 0.4, 40.0)]
		rows = self._written(parts, total_amount=100.0)
		self.assertEqual([r["gross_qty"] for r in rows], [0.6, 0.4])
		self.assertEqual([r["fine_qty"] for r in rows], [0.55, 0.366])
		self.assertEqual([r["amount"] for r in rows], [60.0, 40.0])


class TestTraceabilitySummaryRounding(unittest.TestCase):
	def test_pending_liability_is_the_rows_sum_at_three(self):
		rows = [
			{
				"measure": "Fine g",
				"received": 1.0,
				"owed_back": 1.0,
				"pending_amount": p,
			}
			for p in (0.001, 0.002, 0.004)
		]
		summary = {
			s["label"]: s["value"] for s in report._settlement_summary(rows, True)
		}
		# At 2 decimals this read 0.01 against rows that add up to 0.007.
		self.assertEqual(summary["Pending liability"], 0.007)


class TestRevaluationDeltaRounding(unittest.TestCase):
	"""The custody event records the revaluation's difference at 3 decimals: 7 x 1.001 g."""

	def test_the_delta_keeps_three_decimals(self):
		real_get_value = frappe.db.get_value
		real_cached = frappe.get_cached_value

		def get_value(doctype, *args, **kwargs):
			if doctype == "Batch":
				return "CUST"
			return real_get_value(doctype, *args, **kwargs)

		def cached_value(doctype, *args, **kwargs):
			if doctype == "Company":
				return "INR"
			return real_cached(doctype, *args, **kwargs)

		with patch.object(
			cgv, "is_customer_gold_enabled", return_value=True
		), patch.object(cgv, "is_ledger_schema_ready", return_value=True), patch.object(
			cgv,
			"get_customer_gold_valuation_policy",
			return_value=cgv.VALUATION_NOMINAL,
		), patch.object(cgv.frappe, "has_permission", return_value=None), patch.object(
			cgv.frappe.db, "get_value", side_effect=get_value
		), patch.object(
			cgv.frappe, "get_cached_value", side_effect=cached_value
		), patch.object(cgv, "get_booked_rate", return_value=14622.5), patch.object(
			cgv, "get_returnable_qty", return_value=1.001
		), patch.object(cgv, "_reject_partial"), patch.object(
			cgv, "_reject_zero_value_item"
		), patch.object(cgv, "_reject_non_gold_item"), patch.object(
			cgv, "_resolve_revaluation_event_key", return_value="KEY"
		), patch.object(
			cgv, "_build_revaluation_entry", return_value="SR-1"
		) as build, patch.object(cgv, "_write_event") as write:
			entry, delta = cgv.revalue_customer_gold(
				"CG Co",
				"CUST",
				"B-1",
				"RM - CG",
				"M-G-24KT-99.9-Y",
				posting_date="2026-11-25",
				new_rate=14629.5,
			)

		self.assertEqual((entry, delta), ("SR-1", 7.007))
		self.assertEqual(write.call_args.kwargs["cg_carrying_value_delta"], 7.007)
		build.assert_called_once()


class TestMessagesShowThreeDecimals(unittest.TestCase):
	"""A sample of the refusals: every number is padded to 3 decimals and grouped."""

	def test_an_snc_shortfall_names_both_figures_at_three(self):
		se = MagicMock()
		se.items = [
			frappe._dict(item_code="M", batch_no="B", s_warehouse="W", qty=0.1),
			frappe._dict(item_code="M", batch_no="B", s_warehouse="W", qty=0.2),
		]
		with _number_format(), patch.object(
			snc, "_consumable_batch_qty", return_value=0.25
		):
			with self.assertRaises(frappe.ValidationError) as raised:
				snc._submit_consuming_stock_entry(se)
		self.assertIn("need 0.300, have 0.250", str(raised.exception))
		se.submit.assert_not_called()

	def test_float_dust_in_the_snc_need_is_no_shortfall(self):
		# 0.1 + 0.2 is 0.30000000000000004; a 0.3 balance covers it.
		se = MagicMock()
		se.items = [
			frappe._dict(item_code="M", batch_no="B", s_warehouse="W", qty=0.1),
			frappe._dict(item_code="M", batch_no="B", s_warehouse="W", qty=0.2),
		]
		with patch.object(snc, "_consumable_batch_qty", return_value=0.3):
			snc._submit_consuming_stock_entry(se)
		se.submit.assert_called_once()

	def test_a_hybrid_shortfall_pads_frappes_float_text(self):
		# frappe.format(..., Float) printed "2" and "2.36".
		requirement = frappe._dict(
			problem=None,
			ref_customer="REF",
			pmo="PMO-1",
			customer="CUST",
			finding_category="Chains",
			finding_sub_category=None,
		)
		with _number_format():
			with self.assertRaises(frappe.ValidationError) as raised:
				hybrid_findings.throw_no_customer_batch(
					requirement, "F-1", "WH", 2, 1234.36
				)
		self.assertIn(
			"need 2.000, the order's customer batches hold 1,234.360",
			str(raised.exception),
		)
