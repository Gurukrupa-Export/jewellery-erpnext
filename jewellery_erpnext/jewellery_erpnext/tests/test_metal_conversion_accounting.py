# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""MCON00333's accounting on real documents: value, GL, liability, precision.

MCON00333 (kg-gk, 29 Sep 2026) converted 20 g of customer GJCU0009's 24KT -- the whole 1.327 g of
batch -12 and 18.673 g of batch -13, both booked at 15,190.10/g -- plus 1.798 g of company alloy
at 62.00/g into 21.798 g of 22KT. MAT-STE-19749 moved 303,913.48 out and 303,913.48 in, and posted
no GL row at all. The meeting read that zero as "no accounting effect". It is equal value on ONE
stock account, netted away by ERPNext's GL merge -- and this suite pins that, and everything the
zero hides, on real Metal Conversions documents:

* value: out equals in, no value difference, no additional cost, each lane valued from its inputs;
* GL: none on one stock account although the Stock Ledger and the bundles move; two balanced lines
  when the target warehouse maps to another account; still none for another warehouse on the same
  account; a Stock Adjustment line only when a produce row carries a hand-set rate;
* liability: neither the liability account nor the customer's position moves, and the conversion's
  events carry the same fine gold out and in, at no value; the alloy stays the company's;
* settlement: delivering part of the converted 22KT releases the customer's booked gold for the fine
  gold delivered -- never the 22KT's carrying value, whose company alloy stays out of the liability;
* precision: the header keeps 21.798365123 g, the rows book 21.798 g, pure quantity stays 20.000;
* cancel and amend post once; a mixed-owner conversion shifts no value between owners; a repost
  leaves the conversion whole.

WHERE IT RUNS
-------------
Only on the disposable site (``customer_gold_disposable_site``), on the fixtures of
``test_metal_conversion_batch_isolation``: the Nominal policy and the real ``Repack-Metal
Conversion`` type, created inside the class transaction. ``IntegrationTestCase`` rolls back once
per CLASS and the site carries other suites' committed residue, so every figure here is one
voucher's own or a before/after DELTA, every test takes fresh warehouses and batches, and no
fixture issues DDL. Not in CI; ``inventory-tests.yml`` carries the command.

WHAT IS ONLY PINNED
-------------------
Two rules are undecided: whether a conversion may target another warehouse (only the form keeps
the two equal) and whether a produce row may carry a hand-set rate (R28). The tests on those
shapes say "pins current behaviour": they record what ERPNext does today, not that it is right,
and change when the rule is decided.

HOW THE EXPECTATIONS ARE MADE
-----------------------------
Quantities are MCON00333's own, written out by hand: 20 g x 100 / 91.75 = 21.798365123 g, booked
21.798 g; per batch 1.327 + 0.119 = 1.446 g and 18.673 + 1.679 = 20.352 g. Rates are the site's --
the receipts post the suite's 7,164.83/g, since the site's feed would refuse 15,190.10 as an
outlier -- and every value is computed from what the receipts POSTED, never from the code under
test.
"""

from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from unittest.mock import patch

import frappe
from erpnext.stock import get_warehouse_account_map
from frappe.utils import add_days, flt, nowdate

from jewellery_erpnext.customer_subcontracting import (
	customer_gold_fulfilment as cgf,
)
from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	VALUATION_NOMINAL,
	get_customer_gold_valuation_policy,
)
from jewellery_erpnext.jewellery_erpnext.customization.stock_entry import (
	stock_entry as custom_stock_entry,
)
from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions import (
	lane_tag,
)

from .test_customer_gold_integration import (
	ABBR,
	COMPANY,
	CUSTOMER,
	OTHER_CUSTOMER,
	RAW_RATE,
	SALES_TYPE,
)
from .test_metal_conversion_batch_isolation import (
	ALLOY_ITEM,
	ALLOY_RATE,
	COMPANY_RECEIPT_TYPE,
	MC_SE_TYPE,
	PAISA,
	RAW_RATE_12,
	REGULAR_RATE,
	SOURCE_ITEM,
	TARGET_ITEM,
	_d,
	_MetalConversionCase,
)

#: MCON00333's draw: 20 g, the whole of batch -12 and part of batch -13.
SOURCE_QTY = 20
BATCH_12_QTY = 1.327
BATCH_13_QTY = 20.0
ALLOY_STOCK = 2.0
ALLOY_QTY = 1.798
#: 20 g x 100 / 91.75: kept on the header unrounded, booked by the rows at 3 places.
HEADER_TARGET_QTY = 21.798365123
BOOKED_TARGET_QTY = 21.798
#: The fine gold of 20 g at 100%, which the conversion must neither add to nor take from.
SOURCE_FINE = 20.0

#: kg-gk values these items at Moving Average; the site's default is FIFO.
VALUATION_METHOD = "Moving Average"

#: MCON00333's forced 14,000/g against its 15,190.10/g source, scaled to the 7,164.83/g this
#: suite's receipts post: 14,000 x 7,164.83 / 15,190.10 = 6,603.486 -> 6,603.49.
FORCED_22KT_RATE = 6603.49


class _MCON00333Case(_MetalConversionCase):
	"""The isolation suite's fixtures, with every setting the GL outcome depends on stated."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._pin_accounting()

	@classmethod
	def _pin_accounting(cls):
		"""Pin what the zero GL rests on, in the class transaction (DML only).

		Perpetual inventory, and accounts per WAREHOUSE: with item-wise inventory accounts the
		account would follow the item, and the 24KT and 22KT rows could post to different ones.
		The pure gold item is the 100% source, as kg-gk's 24KT reads 100, so pure quantity is
		fine gold and 20 g of 24KT is 20.000 pure.
		"""
		frappe.db.set_value(
			"Company",
			COMPANY,
			{"enable_perpetual_inventory": 1, "enable_item_wise_inventory_account": 0},
		)
		for item_code in (SOURCE_ITEM, TARGET_ITEM, ALLOY_ITEM):
			frappe.db.set_value("Item", item_code, "valuation_method", VALUATION_METHOD)
		frappe.db.set_value(
			"Manufacturing Setting",
			cls.manufacturing_setting,
			"pure_gold_item",
			SOURCE_ITEM,
		)
		cls.stock_account = frappe.db.get_value(
			"Company", COMPANY, "default_inventory_account"
		)
		cls.adjustment_account = frappe.db.get_value(
			"Company", COMPANY, "stock_adjustment_account"
		)
		cls.other_stock_account = cls._ensure_stock_account(
			"CG Test Conversion Target Stock"
		)

	@classmethod
	def _ensure_stock_account(cls, name):
		"""A second leaf Stock account, beside the company's own inventory account."""
		full = f"{name} - {ABBR}"
		if not frappe.db.exists("Account", full):
			frappe.get_doc(
				{
					"doctype": "Account",
					"account_name": name,
					"company": COMPANY,
					"parent_account": frappe.db.get_value(
						"Account", cls.stock_account, "parent_account"
					),
					"root_type": "Asset",
					"account_type": "Stock",
					"is_group": 0,
				}
			).insert(ignore_permissions=True)
		return full

	@classmethod
	def _warehouse_on(cls, account):
		"""A fresh warehouse whose stock account is stated, not inherited from the company."""
		while True:
			cls._warehouses += 1
			name = f"CG MCA {cls.__name__[:22]} {cls._warehouses}"
			if not frappe.db.exists("Warehouse", f"{name} - {ABBR}"):
				break
		frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": name,
				"company": COMPANY,
				"is_group": 0,
				"account": account,
			}
		).insert(ignore_permissions=True)
		return f"{name} - {ABBR}"

	@staticmethod
	def _account_of(warehouse):
		"""The stock account ERPNext itself posts ``warehouse`` to."""
		return get_warehouse_account_map(COMPANY)[warehouse].account

	# ------------------------------------------------------------------ custody
	@staticmethod
	def _account_balance(account):
		return flt(
			frappe.db.sql(
				"""SELECT COALESCE(SUM(credit - debit), 0) FROM `tabGL Entry`
				   WHERE account = %s AND is_cancelled = 0""",
				(account,),
			)[0][0],
			2,
		)

	@classmethod
	def _custody(cls):
		"""What the customer is owed, on every basis a conversion must leave alone."""
		return frappe._dict(
			liability=cls._account_balance(cls.liability_account),
			fine=cgf.get_customer_gold_fine_position(COMPANY, CUSTOMER),
			source=cgf.get_customer_gold_position(COMPANY, CUSTOMER, SOURCE_ITEM),
			target=cgf.get_customer_gold_position(COMPANY, CUSTOMER, TARGET_ITEM),
		)

	# --------------------------------------------------------------- the posting
	@classmethod
	def _mcon00333(cls, target_account=None):
		"""Receive MCON00333's inputs into a fresh warehouse, convert them, return every part.

		Batch -12 holds exactly 1.327 g and is drawn whole; batch -13 holds 20 g and gives up
		18.673 g (a partial conversion). 2 g of company alloy stands by for the 1.798 g needed.
		With ``target_account`` the converted metal lands in a second fresh warehouse on that
		account: the server accepts it, and only the form keeps the two warehouses equal.
		"""
		wh = cls._warehouse_on(cls.stock_account)
		target_wh = cls._warehouse_on(target_account) if target_account else wh
		b12, r12 = cls._customer_batch(wh, BATCH_12_QTY)
		b13, r13 = cls._customer_batch(wh, BATCH_13_QTY)
		alloy, r_alloy = cls._company_batch(wh, ALLOY_ITEM, ALLOY_STOCK, ALLOY_RATE)

		before = cls._custody()
		mc = cls._conversion(wh, SOURCE_QTY)
		if target_wh != wh:
			mc.target_warehouse = target_wh
			mc.save(ignore_permissions=True)
		mc.submit()
		(se_name,) = cls._entries(mc)
		return frappe._dict(
			wh=wh,
			target_wh=target_wh,
			b12=b12,
			r12=r12,
			b13=b13,
			r13=r13,
			alloy=alloy,
			r_alloy=r_alloy,
			mc=mc,
			se=frappe.get_doc("Stock Entry", se_name),
			before=before,
			after=cls._custody(),
		)

	# ------------------------------------------------------------------ reading
	@staticmethod
	def _gl(voucher_no):
		"""Every GL Entry of the voucher, cancelled or not, so no reversal can hide a row."""
		return frappe.get_all(
			"GL Entry",
			filters={"voucher_type": "Stock Entry", "voucher_no": voucher_no},
			fields=["account", "debit", "credit", "is_cancelled"],
			order_by="account",
		)

	@staticmethod
	def _lines(gles):
		return sorted((g.account, flt(g.debit, 2), flt(g.credit, 2)) for g in gles)

	@staticmethod
	def _events(voucher_no):
		return frappe.get_all(
			"Customer Gold Ledger Entry",
			filters={"reference_docname": voucher_no},
			fields=[
				"name",
				"cg_event_kind",
				"batch_no",
				"customer",
				"cg_gross_qty_delta",
				"cg_fine_gold_delta",
				"cg_carrying_value_delta",
				"cg_reversal_of",
			],
		)

	def _sle_value(self, se, rows):
		return sum(_d(self._sle(se, row).stock_value_difference) for row in rows)

	def _assert_each_lane_posts_what_it_consumed(self, se):
		"""Inside every lane, value in equals value out to the paisa: none crosses lanes."""
		for tag, group in self._groups(se).items():
			with self.subTest(lane=tag):
				out = -self._sle_value(se, group["sources"] + group["alloy"])
				into = self._sle_value(se, group["targets"])
				self.assertLessEqual(abs(out - into), PAISA)


class TestMCON00333Conversion(_MCON00333Case):
	"""The reported conversion as it ran: one warehouse, one stock account."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.conv = cls._mcon00333()

	def setUp(self):
		self.se = self._entry(self.conv.mc)

	def test_the_fixture_is_mcon00333s_shape(self):
		"""Guard the guard: the whole of -12 and 18.673 g of -13 at one rate, 1.798 g of alloy."""
		conv = self.conv
		self.assertEqual(
			[(row.batch, flt(row.qty, 3)) for row in conv.mc.source_batch_details],
			[(conv.b12, 1.327), (conv.b13, 18.673)],
		)
		self.assertEqual(conv.mc.source_alloy_qty, "1.798")
		self.assertEqual(conv.r12, conv.r13)
		self.assertEqual(self._account_of(conv.wh), self.stock_account)

	def test_value_leaves_and_arrives_whole(self):
		"""What the receipts posted for the grams drawn leaves, and exactly that arrives.

		Nothing is added on the way (no additional cost), the voucher's value difference is 0,
		and each lane's target carries only its own inputs. -12 is used up; -13 keeps 1.327 g.
		"""
		conv, se = self.conv, self.se
		rates = {conv.b12: conv.r12, conv.b13: conv.r13, conv.alloy: conv.r_alloy}
		expected = sum(
			(_d(row.qty) * rates[self._bundle_batch(row)]).quantize(
				PAISA, rounding=ROUND_HALF_UP
			)
			for row in se.items
			if row.s_warehouse
		)
		self.assertLessEqual(abs(_d(se.total_outgoing_value) - expected), PAISA)
		self.assertEqual(
			flt(se.total_incoming_value, 2), flt(se.total_outgoing_value, 2)
		)
		self.assertEqual(flt(se.value_difference, 2), 0.0)
		self.assertEqual(flt(se.total_additional_costs, 2), 0.0)
		self._assert_each_lane_posts_what_it_consumed(se)
		self.assertAlmostEqual(self._balance(conv.b12, conv.wh), 0.0, places=3)
		self.assertAlmostEqual(
			self._balance(conv.b13, conv.wh), BATCH_13_QTY - 18.673, places=3
		)

	def test_one_stock_account_posts_no_gl_although_stock_moved(self):
		"""MAT-STE-19749's empty GL, and why it is empty.

		ERPNext writes a stock line and a Stock Adjustment line per ledger row, merges lines on
		one account and drops what nets to 0.00. Every row here is on one account and the
		rounded values net to nothing, so no GL row survives -- cancelled or not -- while the
		Stock Ledger and a bundle per row still record every gram and rupee that moved.
		"""
		se = self.se
		self.assertEqual(self._gl(se.name), [])

		sles = frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_no": se.name, "is_cancelled": 0},
			fields=["warehouse", "stock_value_difference"],
		)
		self.assertEqual(len(sles), len(se.items))
		self.assertTrue(all(flt(sle.stock_value_difference, 2) for sle in sles), sles)
		self.assertEqual(
			{self._account_of(sle.warehouse) for sle in sles}, {self.stock_account}
		)
		self.assertEqual(
			flt(sum(flt(sle.stock_value_difference, 2) for sle in sles), 2), 0.0
		)
		bundles = frappe.get_all(
			"Serial and Batch Bundle",
			filters={
				"voucher_type": "Stock Entry",
				"voucher_no": se.name,
				"docstatus": 1,
				"is_cancelled": 0,
			},
			pluck="name",
		)
		self.assertEqual(len(bundles), len(se.items))

	def test_the_liability_and_the_customers_holding_do_not_move(self):
		"""The customer is owed what they were owed before: 20 g fine, at the receipts' value.

		The liability account's balance and the customer's fine and per-item positions are
		unchanged (a conversion is not a position kind), and the conversion's own events carry
		the same 20.000 g fine out and in at no value -- the extra 1.798 g is the company's alloy.
		"""
		before, after = self.conv.before, self.conv.after
		self.assertEqual(after.liability, before.liability)
		self.assertAlmostEqual(after.fine, before.fine, places=3)
		self.assertAlmostEqual(after.source, before.source, places=3)
		self.assertAlmostEqual(after.target, before.target, places=3)

		events = self._events(self.se.name)
		self.assertEqual(
			{e.cg_event_kind for e in events}, {"Conversion Out", "Conversion In"}
		)
		self.assertEqual({e.customer for e in events}, {CUSTOMER})
		fine_out = sum(
			flt(e.cg_fine_gold_delta)
			for e in events
			if e.cg_event_kind == "Conversion Out"
		)
		fine_in = sum(
			flt(e.cg_fine_gold_delta)
			for e in events
			if e.cg_event_kind == "Conversion In"
		)
		self.assertAlmostEqual(fine_out, -SOURCE_FINE, places=3)
		self.assertAlmostEqual(fine_in, SOURCE_FINE, places=3)
		for event in events:
			self.assertEqual(flt(event.cg_carrying_value_delta, 2), 0.0)

	def test_the_company_alloy_stays_the_companys(self):
		"""1.798 g of company alloy joined the customer's metal without becoming the customer's.

		Its rows stay Regular Stock with no customer (only the lane tag says which metal it
		joined), its batch writes no Customer Gold event, and the targets' Batch Components list
		it as company material beside the customer's 20 g. Its value is counted once: the
		targets hold the customer's gold plus exactly the alloy's.
		"""
		conv, se = self.conv, self.se
		alloy_rows = [row for row in se.items if row.item_code == ALLOY_ITEM]
		self.assertTrue(alloy_rows)
		for row in alloy_rows:
			self.assertEqual(
				(row.inventory_type, row.customer or None), ("Regular Stock", None)
			)
			self.assertTrue(
				row.custom_conversion_lane.startswith(f"Customer Goods|{CUSTOMER}|")
			)
		self.assertAlmostEqual(
			sum(flt(row.qty) for row in alloy_rows), ALLOY_QTY, places=3
		)
		self.assertAlmostEqual(
			self._balance(conv.alloy, conv.wh), ALLOY_STOCK - ALLOY_QTY, places=3
		)
		self.assertNotIn(conv.alloy, {e.batch_no for e in self._events(se.name)})

		targets = [row for row in se.items if row.t_warehouse]
		components = [
			c for row in targets for c in self._components(self._bundle_batch(row))
		]
		alloy = [c for c in components if c.source_batch == conv.alloy]
		self.assertEqual(
			{(c.inventory_type, c.customer) for c in alloy}, {("Regular Stock", None)}
		)
		self.assertAlmostEqual(sum(flt(c.qty) for c in alloy), ALLOY_QTY, places=3)
		customer = [c for c in components if c.customer]
		self.assertEqual(
			{(c.customer, c.inventory_type) for c in customer},
			{(CUSTOMER, "Customer Goods")},
		)
		self.assertAlmostEqual(sum(flt(c.qty) for c in customer), SOURCE_QTY, places=3)

		gold = -self._sle_value(
			se, [r for r in se.items if r.s_warehouse and r.item_code == SOURCE_ITEM]
		)
		self.assertLessEqual(
			abs(self._sle_value(se, targets) - gold + self._sle_value(se, alloy_rows)),
			PAISA * len(targets),
		)

	def test_the_header_keeps_21_798365123_g_while_the_rows_book_21_798_g(self):
		"""The 0.000365 g lives only on the conversion; the rows post at transfer_qty's 3 places.

		Pure quantity is still the source's fine gold: 21.798 g x 91.75% = 19.999665 -> 20.000,
		the 20 g at 100% that went in (the site convention: "99.9" reads 100). The Conversion In
		events carry the same 20.000 g.
		"""
		se = self.se
		header = flt(
			frappe.db.get_value("Metal Conversions", self.conv.mc.name, "target_qty"), 9
		)
		self.assertAlmostEqual(header, HEADER_TARGET_QTY, places=9)
		targets = [row for row in se.items if row.t_warehouse]
		booked = sum(flt(row.qty) for row in targets)
		self.assertAlmostEqual(booked, BOOKED_TARGET_QTY, places=6)
		self.assertAlmostEqual(header - booked, 0.000365123, places=9)
		self.assertAlmostEqual(
			sum(flt(self._sle(se, row).actual_qty) for row in targets),
			BOOKED_TARGET_QTY,
			places=6,
		)

		sources = [
			row for row in se.items if row.s_warehouse and row.item_code == SOURCE_ITEM
		]
		self.assertAlmostEqual(
			sum(flt(row.custom_pure_qty) for row in sources), SOURCE_FINE, places=3
		)
		self.assertAlmostEqual(
			sum(flt(row.custom_pure_qty) for row in targets), SOURCE_FINE, places=3
		)
		self.assertAlmostEqual(
			sum(
				flt(e.cg_fine_gold_delta)
				for e in self._events(se.name)
				if e.cg_event_kind == "Conversion In"
			),
			SOURCE_FINE,
			places=3,
		)


class TestMCON00333Warehouses(_MCON00333Case):
	"""The target in another warehouse. PINS CURRENT BEHAVIOUR; the same-warehouse rule is
	undecided -- the server accepts any target warehouse and only the form keeps them equal."""

	def test_a_target_on_another_account_posts_two_balanced_lines(self):
		"""Pins current behaviour; the same-warehouse rule is undecided.

		The value leaves one stock account and arrives on another: Cr source / Dr target for
		the whole moved value, a balance-sheet transfer. No Stock Adjustment line, because the
		value is equal -- a rate change would be the only thing that could put one there.
		"""
		conv = self._mcon00333(target_account=self.other_stock_account)
		se = conv.se
		self.assertEqual(self._account_of(conv.target_wh), self.other_stock_account)

		moved = flt(se.total_incoming_value, 2)
		self.assertGreater(moved, 0)
		self.assertEqual(
			self._lines(self._gl(se.name)),
			sorted(
				[
					(self.stock_account, 0.0, moved),
					(self.other_stock_account, moved, 0.0),
				]
			),
		)
		self.assertEqual(flt(se.value_difference, 2), 0.0)
		self.assertEqual(
			{row.s_warehouse for row in se.items if row.s_warehouse}, {conv.wh}
		)
		self.assertEqual(
			{row.t_warehouse for row in se.items if row.t_warehouse}, {conv.target_wh}
		)
		self.assertEqual(conv.after.liability, conv.before.liability)

	def test_another_warehouse_on_the_same_account_still_posts_no_gl(self):
		"""Pins current behaviour; the same-warehouse rule is undecided.

		Two warehouses on one account, as kg-gk's Waxing RM and Waxing RSV: the stock moves
		between them and the GL is still empty. The zero comes from the account, not from the
		warehouses being the same.
		"""
		conv = self._mcon00333(target_account=self.stock_account)
		se = conv.se
		self.assertNotEqual(conv.target_wh, conv.wh)
		self.assertEqual(self._account_of(conv.target_wh), self.stock_account)

		self.assertEqual(self._gl(se.name), [])
		self.assertEqual(
			set(
				frappe.get_all(
					"Stock Ledger Entry",
					filters={"voucher_no": se.name, "is_cancelled": 0},
					pluck="warehouse",
				)
			),
			{conv.wh, conv.target_wh},
		)
		self.assertEqual(flt(se.value_difference, 2), 0.0)


class TestMCON00333ManualRate(_MCON00333Case):
	"""A hand-set rate on the produce row. PINS CURRENT BEHAVIOUR; policy R28 (manual rates on
	conversions) is undecided."""

	def _hand_priced_conversion(self, wh, sources, alloy, rate):
		"""MAT-STE-19749's rows as one lane, with the 22KT row priced by hand.

		No builder does this -- Metal Conversions leaves every rate to ERPNext and the lane
		pricer -- so the entry is built directly, as any hand-made conversion entry would be.
		The pricer never takes over a row with ``set_basic_rate_manually``.
		"""
		tag = lane_tag("Customer Goods", CUSTOMER)
		common = {
			"uom": "Gram",
			"stock_uom": "Gram",
			"conversion_factor": 1,
			"custom_conversion_lane": tag,
			"expense_account": self.difference_account,
		}
		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = MC_SE_TYPE
		se.purpose = "Repack"
		se.company = COMPANY
		se.inventory_type = "Customer Goods"
		se._customer = CUSTOMER
		se.auto_created = 1
		for batch, qty in sources:
			se.append(
				"items",
				dict(
					common,
					item_code=SOURCE_ITEM,
					qty=qty,
					batch_no=batch,
					use_serial_batch_fields=1,
					s_warehouse=wh,
					inventory_type="Customer Goods",
					customer=CUSTOMER,
				),
			)
		se.append(
			"items",
			dict(
				common,
				item_code=ALLOY_ITEM,
				qty=alloy[1],
				batch_no=alloy[0],
				use_serial_batch_fields=1,
				s_warehouse=wh,
				inventory_type="Regular Stock",
			),
		)
		se.append(
			"items",
			dict(
				common,
				item_code=TARGET_ITEM,
				qty=BOOKED_TARGET_QTY,
				t_warehouse=wh,
				inventory_type="Customer Goods",
				customer=CUSTOMER,
				set_basic_rate_manually=1,
				basic_rate=rate,
			),
		)
		se.flags.ignore_mandatory = True
		se.save()
		se.submit()
		return se

	def test_a_manual_rate_posts_the_difference_to_stock_adjustment(self):
		"""Pins current behaviour; policy R28 (manual rates on conversions) undecided.

		MCON00333's claimed 14,000/g, scaled to this site: 21.798 g at 6,603.49 is booked in
		against what the 20 g and the alloy gave up. ERPNext keeps the hand-set rate, so the
		entry is no longer value-neutral and the difference goes to Stock Adjustment: Dr stock /
		Cr Stock Adjustment. It is company profit and loss -- the liability and the customer's
		events carry none of it. (At MCON00333's own figures: 1,258.52.)
		"""
		wh = self._warehouse_on(self.stock_account)
		b12, _ = self._customer_batch(wh, BATCH_12_QTY)
		b13, _ = self._customer_batch(wh, BATCH_13_QTY)
		alloy, _ = self._company_batch(wh, ALLOY_ITEM, ALLOY_STOCK, ALLOY_RATE)
		liability = self._account_balance(self.liability_account)

		se = self._hand_priced_conversion(
			wh, [(b12, 1.327), (b13, 18.673)], (alloy, ALLOY_QTY), FORCED_22KT_RATE
		)

		(target,) = [row for row in se.items if row.t_warehouse]
		self.assertEqual(flt(target.basic_rate, 2), FORCED_22KT_RATE)
		incoming = flt(BOOKED_TARGET_QTY * FORCED_22KT_RATE, 2)
		variance = flt(incoming - flt(se.total_outgoing_value, 2), 2)
		self.assertGreater(variance, 0)
		self.assertEqual(flt(se.total_incoming_value, 2), incoming)
		self.assertEqual(flt(se.value_difference, 2), variance)
		self.assertEqual(
			self._lines(self._gl(se.name)),
			sorted(
				[
					(self.adjustment_account, 0.0, variance),
					(self.stock_account, variance, 0.0),
				]
			),
		)
		self.assertEqual(self._account_balance(self.liability_account), liability)
		events = self._events(se.name)
		self.assertTrue(events)
		for event in events:
			self.assertEqual(flt(event.cg_carrying_value_delta, 2), 0.0)


class TestMCON00333Lifecycle(_MCON00333Case):
	"""Cancel, amend and resubmit post once. The target sits on another account so the entry has
	GL rows to double."""

	def _footprint(self, voucher_no):
		"""What one posting left: live ledger rows, live GL lines, Customer Gold events by kind."""
		return frappe._dict(
			sles=frappe.db.count(
				"Stock Ledger Entry", {"voucher_no": voucher_no, "is_cancelled": 0}
			),
			gl=self._lines([g for g in self._gl(voucher_no) if not g.is_cancelled]),
			events=Counter(e.cg_event_kind for e in self._events(voucher_no)),
		)

	def test_cancel_and_amend_post_the_conversion_once(self):
		"""Cancelling reverses every row once; the amendment posts the same footprint again, once.

		The cancelled entry keeps no live Stock Ledger or GL row, and each of its conversion
		events gets exactly one reversal. The amended conversion makes one new entry -- a retried
		submit adds nothing -- with the same ledger rows, GL lines and events as the first, and
		consumes the sources once.
		"""
		conv = self._mcon00333(target_account=self.other_stock_account)
		first = self._footprint(conv.se.name)
		self.assertEqual(len(first.gl), 2)
		self.assertTrue(first.events)

		frappe.get_doc("Metal Conversions", conv.mc.name).cancel()
		old = conv.se.name
		self.assertEqual(frappe.db.get_value("Stock Entry", old, "docstatus"), 2)
		cancelled = self._footprint(old)
		self.assertEqual((cancelled.sles, cancelled.gl), (0, []))
		net = {}
		for g in self._gl(old):
			net[g.account] = flt(
				net.get(g.account, 0) + flt(g.debit) - flt(g.credit), 2
			)
		self.assertTrue(all(value == 0 for value in net.values()), net)
		events = self._events(old)
		reversed_ = Counter(
			e.cg_reversal_of for e in events if e.cg_event_kind == "Reversal"
		)
		originals = [e.name for e in events if e.cg_event_kind != "Reversal"]
		self.assertEqual(reversed_, Counter(originals))

		# As the desk's Amend does, less the cancelled entry's link (metal_conversions.js).
		amended = frappe.copy_doc(frappe.get_doc("Metal Conversions", conv.mc.name))
		amended.amended_from = conv.mc.name
		amended.docstatus = 0
		amended.stock_entry = None
		amended.insert(ignore_permissions=True)
		amended.submit()
		try:
			frappe.get_doc("Metal Conversions", amended.name).submit()
		except frappe.ValidationError:
			pass

		entries = self._entries(amended)
		self.assertEqual(len(entries), 1)
		self.assertNotEqual(entries[0], old)
		self.assertEqual(self._footprint(entries[0]), first)
		self.assertAlmostEqual(self._balance(conv.b12, conv.wh), 0.0, places=3)
		self.assertAlmostEqual(
			self._balance(conv.b13, conv.wh), BATCH_13_QTY - 18.673, places=3
		)


class TestMixedOwnerConversion(_MCON00333Case):
	"""Two customers and company metal in one conversion (R9: MAT-STE-18032 once shifted ~165.6k)."""

	def test_each_owner_keeps_the_value_of_its_own_metal(self):
		"""Each lane's target is valued from its own source and its own alloy share -- no value
		crosses owners. The two customers' gold is received at different rates, so a pooled
		rate misses by rupees, not paise."""
		wh = self._warehouse_on(self.stock_account)
		a, rate_a = self._customer_batch(wh, 4.0, customer=CUSTOMER)
		b, rate_b = self._customer_batch(
			wh, 3.0, customer=OTHER_CUSTOMER, raw_rate=RAW_RATE_12
		)
		reg, rate_reg = self._company_batch(wh, SOURCE_ITEM, 2.0, REGULAR_RATE)
		_alloy, rate_alloy = self._company_batch(wh, ALLOY_ITEM, 1.0, ALLOY_RATE)
		self._ensure_gold_rate(nowdate(), RAW_RATE)
		self.assertNotEqual(rate_a, rate_b)

		mc = self._conversion(wh, 9)
		mc.submit()
		se = self._entry(mc)

		for source, owner, rate in (
			(a, CUSTOMER, rate_a),
			(b, OTHER_CUSTOMER, rate_b),
			(reg, None, rate_reg),
		):
			with self.subTest(source=source):
				_tag, group = self._group_of_source(se, source)
				(target,) = group["targets"]
				self.assertEqual(
					frappe.db.get_value(
						"Batch", self._bundle_batch(target), "custom_customer"
					),
					owner,
				)
				expected = (
					sum(_d(r.qty) for r in group["sources"]) * rate
					+ sum(_d(r.qty) for r in group["alloy"]) * rate_alloy
				).quantize(PAISA, rounding=ROUND_HALF_UP)
				self.assertLessEqual(
					abs(self._sle_value(se, group["targets"]) - expected), PAISA
				)
		self._assert_each_lane_posts_what_it_consumed(se)
		self.assertLessEqual(abs(_d(se.value_difference)), PAISA * 3)


class TestMCON00333Repost(_MCON00333Case):
	"""A repost reaches the conversion and leaves it whole (Phase 7 #15).

	A Metal Conversion cannot itself be backdated: its builder posts at submit time. What
	reposts one in production is an entry backdated before it on an item it consumed -- ERPNext
	then re-derives the conversion's rates from its stored ledger values. Here that is a company
	receipt of the 24KT item dated the day before; the repost runs synchronously under tests.
	"""

	def _backdated_receipt(self, warehouse, item_code, qty, rate, posting_date):
		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = COMPANY_RECEIPT_TYPE
		se.purpose = "Material Receipt"
		se.company = COMPANY
		se.posting_date = posting_date
		se.posting_time = "12:00:00"
		se.set_posting_time = 1
		se.append(
			"items",
			{
				"item_code": item_code,
				"qty": qty,
				"basic_rate": flt(rate, 9),
				"t_warehouse": warehouse,
				"uom": "Gram",
				"stock_uom": "Gram",
				"conversion_factor": 1,
				"inventory_type": "Regular Stock",
				"expense_account": self.difference_account,
			},
		)
		se.flags.ignore_mandatory = True
		se.save()
		se.submit()
		return se

	def test_a_backdated_entry_reposts_the_conversion_and_it_stays_whole(self):
		"""The repost re-prices the conversion; value stays conserved, the rate within 0.005.

		A repost resets each consumed row's rate to its ledger value over its quantity, so the
		derived 22KT rate may move by a fraction of a paisa per gram -- hence the tolerance on the
		rate, and exact comparisons on the amounts.
		"""
		if not frappe.in_test:
			self.skipTest(
				"Repost Item Valuation runs synchronously only under the test runner"
			)

		conv = self._mcon00333()
		se = conv.se
		targets = [row for row in se.items if row.t_warehouse]
		rates = {row.name: flt(self._sle(se, row).incoming_rate) for row in targets}

		priced = []
		real_pricer = custom_stock_entry.set_process_loss_produce_rates

		def _pricer(entry):
			priced.append(entry.name)
			return real_pricer(entry)

		with patch.object(
			custom_stock_entry, "set_process_loss_produce_rates", side_effect=_pricer
		):
			self._backdated_receipt(
				conv.wh, SOURCE_ITEM, 1.0, REGULAR_RATE, add_days(nowdate(), -1)
			)

		reposts = frappe.get_all(
			"Repost Item Valuation",
			filters={"item_code": SOURCE_ITEM, "warehouse": conv.wh, "docstatus": 1},
			pluck="status",
		)
		self.assertTrue(reposts, "the backdated receipt queued no repost")
		self.assertEqual(set(reposts), {"Completed"})
		self.assertIn(se.name, priced, "the repost never re-priced the conversion")

		se = frappe.get_doc("Stock Entry", se.name)
		self.assertEqual(
			flt(se.total_incoming_value, 2), flt(se.total_outgoing_value, 2)
		)
		self.assertEqual(flt(se.value_difference, 2), 0.0)
		self._assert_each_lane_posts_what_it_consumed(se)
		for row in targets:
			with self.subTest(row=row.name):
				self.assertAlmostEqual(
					flt(self._sle(se, row).incoming_rate), rates[row.name], places=2
				)
		self.assertEqual(self._gl(se.name), [])


class TestMCON00333Delivery(_MCON00333Case):
	"""Delivering part of the converted 22KT releases only the customer's booked gold (Phase 7).

	The conversion leaves the liability alone (``TestMCON00333Conversion``), so it is the delivery
	of the 22KT that must release it -- and release only what the customer handed over. At
	MCON00333's own figures the whole 21.798 g would release 303,802.00, the 20 g fine at the
	receipts' booked 15,190.10/g: never its 303,913.48 carrying value, which holds 111.48 of
	company alloy, nor 21.798 g at the 24KT rate, 331,113.80.

	Per-batch lanes put receipt -13's 18.673 g and 1.679 g of company alloy into one 20.352 g
	batch of 22KT. Ten grams of it ship on a Sales Order and a Delivery Note, under the Nominal
	policy the fixtures set inside the class transaction. The batch's customer components say
	whose gold it holds -- receipt -13's alone -- and the ten grams carry it pro rata, each
	component at its own receipt's booked rate::

	    customer fine gold   18.673 x 10 / 20.352 g               =      9.175 g
	    released             9.17502 g x 7,164.83                 =  65,737.46
	    carrying value       the same plus 0.825 g of alloy at 62 =  65,788.60
	    ten grams at 24KT    10 g x 7,164.83                      =  71,648.30

	Only the released line is the customer's. It follows the lane's recorded make-up -- 18.673 of
	20.352 g, 91.7502% customer gold -- not the item's nominal 91.75%, which would say 65,737.32.
	"""

	#: Stock quantities persist at 2 places on this site (only transfer_qty and the bundle carry
	#: 3), so the part delivered is a whole 10 g.
	DELIVERED_QTY = 10.0
	#: Lane -13's make-up, written out by hand: 18.673 g of the customer's 24KT and its share of
	#: the alloy, 1.798 x 18.673 / 20 = 1.679 g -- 20.352 g of 22KT.
	LANE_GOLD = 18.673
	LANE_ALLOY = 1.679
	LANE_QTY = 20.352

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.conv = cls._mcon00333()
		cls.batch = cls._target_made_from(cls.conv.se, cls.conv.b13)
		cls.before = cls._custody()
		cls.dn = cls._deliver(cls.conv.wh, cls.batch, cls.DELIVERED_QTY)
		cls.after = cls._custody()

	@classmethod
	def _target_made_from(cls, se, source_batch):
		"""The 22KT batch that ``source_batch``'s lane of ``se`` produced."""
		(lane,) = {
			row.custom_conversion_lane
			for row in se.items
			if row.s_warehouse and cls._bundle_batch(row) == source_batch
		}
		(target,) = [
			row
			for row in se.items
			if row.t_warehouse and row.custom_conversion_lane == lane
		]
		return cls._bundle_batch(target)

	@classmethod
	def _deliver(cls, warehouse, batch_no, qty):
		"""``qty`` of the 22KT in ``batch_no``, on a Sales Order and its Delivery Note.

		``TestConvertedPieceSettlesTheSourceValue._deliver_operating_item``'s path: gke's
		validator refuses a Delivery Note row without its Sales Order, and an Outwork order reads
		the customer's Payment Terms.
		"""
		if not frappe.db.exists("Sales Type", SALES_TYPE):
			frappe.get_doc(
				{"doctype": "Sales Type", "type": SALES_TYPE, "tax_rate": 0}
			).insert(ignore_permissions=True)
		if not frappe.db.exists("Customer Payment Terms", {"customer": CUSTOMER}):
			frappe.get_doc(
				{"doctype": "Customer Payment Terms", "customer": CUSTOMER}
			).insert(ignore_permissions=True)
		row = {
			"item_code": TARGET_ITEM,
			"qty": qty,
			"rate": 0,
			"warehouse": warehouse,
			"uom": "Gram",
			"stock_uom": "Gram",
			"conversion_factor": 1,
		}

		so = frappe.new_doc("Sales Order")
		so.company = COMPANY
		so.customer = CUSTOMER
		so.sales_type = SALES_TYPE
		so.transaction_date = cls.posting_date
		so.delivery_date = cls.posting_date
		so.append("items", dict(row, delivery_date=cls.posting_date))
		so.flags.ignore_mandatory = True
		so.save()
		so.submit()

		dn = frappe.new_doc("Delivery Note")
		dn.company = COMPANY
		dn.customer = CUSTOMER
		dn.posting_date = cls.posting_date
		dn.set_posting_time = 1
		dn.append(
			"items",
			dict(
				row,
				batch_no=batch_no,
				use_serial_batch_fields=1,
				against_sales_order=so.name,
				so_detail=so.items[0].name,
			),
		)
		dn.flags.ignore_mandatory = True
		dn.save()
		dn.submit()
		return dn

	@staticmethod
	def _booked_rate(batch):
		"""What the receipt of ``batch`` booked per gram: its Receipt events' value over grams."""
		receipts = frappe.get_all(
			"Customer Gold Ledger Entry",
			filters={"batch_no": batch, "cg_event_kind": "Receipt"},
			fields=["cg_gross_qty_delta", "cg_carrying_value_delta"],
		)
		return sum(_d(r.cg_carrying_value_delta) for r in receipts) / sum(
			_d(r.cg_gross_qty_delta) for r in receipts
		)

	def _expected(self):
		"""What the ten grams hold, from the lane's make-up and the site's own rates.

		Pro rata over the batch's customer components, each at its own receipt's booked rate;
		the alloy at the rate its company receipt posted.
		"""
		share = _d(self.DELIVERED_QTY) / _d(self.LANE_QTY)
		customer = {self.conv.b13: _d(self.LANE_GOLD)}
		return frappe._dict(
			# 24KT reads 100% here, as on kg-gk: the customer's grams are fine grams.
			fine=sum(qty * share for qty in customer.values()),
			release=sum(
				qty * share * self._booked_rate(source)
				for source, qty in customer.items()
			),
			alloy=_d(self.LANE_ALLOY) * share * self.conv.r_alloy,
		)

	def _delivery_events(self):
		return frappe.get_all(
			"Customer Gold Ledger Entry",
			filters={
				"reference_doctype": "Delivery Note",
				"reference_docname": self.dn.name,
			},
			fields=[
				"cg_event_kind",
				"customer",
				"batch_no",
				"cg_fine_gold_delta",
				"cg_carrying_value_delta",
				"cg_settlement_voucher",
			],
		)

	def _released(self):
		"""What the liability account let go of across the delivery: before less after."""
		return _d(flt(self.before.liability - self.after.liability, 2))

	def test_the_fixture_ships_ten_grams_of_one_lane(self):
		"""Guard the guard: Nominal; receipt -13's 18.673 g beside 1.679 g of company alloy in a
		batch valued on its own; ten of its 20.352 g gone; the receipt booked what it posted."""
		conv = self.conv
		self.assertEqual(get_customer_gold_valuation_policy(), VALUATION_NOMINAL)
		self.assertEqual(
			{
				c.source_batch: (c.customer or None, c.inventory_type, flt(c.qty, 3))
				for c in self._components(self.batch)
			},
			{
				conv.b13: (CUSTOMER, "Customer Goods", self.LANE_GOLD),
				conv.alloy: (None, "Regular Stock", self.LANE_ALLOY),
			},
		)
		# Batch-wise valuation: the ten grams leave at this batch's own rate, not at a
		# warehouse average with the -12 lane's 22KT.
		self.assertEqual(
			frappe.db.get_value("Batch", self.batch, "use_batchwise_valuation"), 1
		)
		self.assertAlmostEqual(
			self._balance(self.batch, conv.wh),
			self.LANE_QTY - self.DELIVERED_QTY,
			places=3,
		)
		self.assertLessEqual(
			abs(self._booked_rate(conv.b13) - conv.r13), Decimal("0.000001")
		)

	def test_the_liability_releases_the_customers_gold_for_the_fine_delivered(self):
		"""The ten grams hold 9.175 g of the customer's fine gold; the liability lets go of
		exactly that, at receipt -13's booked rate.

		One Delivery event carries the fine grams and the value; one Journal Entry debits the
		liability and credits COGS Adjustment by the same amount; the liability account and the
		customer's fine position fall by exactly that, and by nothing more.
		"""
		expected = self._expected()
		events = self._delivery_events()
		self.assertEqual([e.cg_event_kind for e in events], ["Delivery"])
		(event,) = events
		self.assertEqual((event.customer, event.batch_no), (CUSTOMER, self.batch))
		self.assertAlmostEqual(
			flt(event.cg_fine_gold_delta), -float(expected.fine), places=3
		)
		self.assertAlmostEqual(
			self.after.fine - self.before.fine, flt(event.cg_fine_gold_delta), places=3
		)

		released = self._released()
		self.assertLessEqual(
			abs(released - expected.release),
			PAISA,
			"the liability did not release the customer's booked gold for the fine delivered",
		)
		self.assertTrue(
			event.cg_settlement_voucher, "the delivery posted no settlement"
		)
		je = frappe.get_doc("Journal Entry", event.cg_settlement_voucher)
		self.assertEqual(je.docstatus, 1)
		self.assertEqual(
			sorted(
				(
					line.account,
					flt(line.debit_in_account_currency, 2),
					flt(line.credit_in_account_currency, 2),
				)
				for line in je.accounts
			),
			sorted(
				[
					(self.liability_account, float(released), 0.0),
					(self.cogs_account, 0.0, float(released)),
				]
			),
		)
		self.assertEqual(flt(event.cg_carrying_value_delta, 2), -float(released))

	def test_neither_the_22kt_carrying_value_nor_its_alloy_is_released(self):
		"""The ten grams left the stock at their carrying value and the liability at the
		customer's share. The gap is the company alloy in them -- 0.825 g at 62.00 -- which the
		invoice recovers and the liability never held. Nor is the release ten grams at the
		24KT's booked rate: the 22KT's gram count priced as the customer's 24KT.
		"""
		expected = self._expected()
		released = self._released()
		carrying = -sum(
			_d(sle.stock_value_difference)
			for sle in frappe.get_all(
				"Stock Ledger Entry",
				filters={"voucher_no": self.dn.name, "is_cancelled": 0},
				fields=["stock_value_difference"],
			)
		)
		self.assertNotAlmostEqual(
			float(released),
			float(carrying),
			delta=1.0,
			msg="the liability released the 22KT's carrying value, company alloy and all",
		)
		self.assertLessEqual(
			abs(carrying - released - expected.alloy),
			2 * PAISA,
			"what the release left out is not exactly the company alloy's share",
		)
		self.assertNotAlmostEqual(
			float(released),
			float(_d(self.DELIVERED_QTY) * self._booked_rate(self.conv.b13)),
			delta=1.0,
			msg="the liability released the 22KT's grams at the 24KT's booked rate",
		)
