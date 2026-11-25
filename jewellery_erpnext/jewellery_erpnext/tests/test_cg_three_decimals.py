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

from jewellery_erpnext.customer_subcontracting import customer_gold_allocations as alloc
from jewellery_erpnext.customer_subcontracting.cg_format import fmt3
from jewellery_erpnext.customer_subcontracting.sub_utils import gold_usage

APP = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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
		self.assertEqual(fmt3(1.5), "1.500")
		self.assertEqual(fmt3(2), "2.000")
		self.assertEqual(fmt3(0.137763), "0.138")
		self.assertEqual(fmt3(-1.2345), "-1.234")
		self.assertEqual(fmt3(None), "0.000")
		self.assertEqual(fmt3("4.3"), "4.300")


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
