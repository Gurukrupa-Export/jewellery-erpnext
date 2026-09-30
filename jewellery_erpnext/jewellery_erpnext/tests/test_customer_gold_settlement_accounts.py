# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""F4 -- the settlement Journal Entry revalidates its accounts, and posts once.

``KGJPL-JE-JE-26-00018`` posted Dr Customer Goods Receive / Cr Advances from Customers: two
liability accounts. Nothing was discharged. The KGJPL settings row was saved, with the flag on,
before the adjustment account had a root-type rule, and a row is never re-checked unless it is
saved again. So the rules now run again at posting time, before anything is inserted, and the
message says where the row is corrected.

Pure-logic per the suite convention: no document is created and every DB read is patched. The
settings-side rules are in ``test_subcontracting_settings``; real JEs are in
``test_customer_gold_integration``.
"""

import unittest
from unittest.mock import MagicMock, patch

import frappe

from jewellery_erpnext.customer_subcontracting import customer_gold_fulfilment as cgf
from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings import (
	subcontracting_settings as settings_module,
)
from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	VALUATION_NOMINAL,
)

COMPANY = "CG Co"
LIABILITY = "Customer Gold Liability - CG"
ADJUSTMENT = "Customer Gold COGS Adjustment - CG"
ADVANCES = "Advances from Customers - CG"

ACCOUNTS = {
	LIABILITY: frappe._dict(
		company=COMPANY, root_type="Liability", is_group=0, disabled=0, account_type=""
	),
	ADJUSTMENT: frappe._dict(
		company=COMPANY, root_type="Expense", is_group=0, disabled=0, account_type=""
	),
	ADVANCES: frappe._dict(
		company=COMPANY, root_type="Liability", is_group=0, disabled=0, account_type=""
	),
	# One account per remaining refusal, so every ``_throw`` site is reached at posting.
	"Group Expense - CG": frappe._dict(
		company=COMPANY, root_type="Expense", is_group=1, disabled=0, account_type=""
	),
	"Disabled Expense - CG": frappe._dict(
		company=COMPANY, root_type="Expense", is_group=0, disabled=1, account_type=""
	),
	"Payable - CG": frappe._dict(
		company=COMPANY,
		root_type="Liability",
		is_group=0,
		disabled=0,
		account_type="Payable",
	),
	"Stock In Hand - CG": frappe._dict(
		company=COMPANY, root_type="Asset", is_group=0, disabled=0, account_type="Stock"
	),
	"Expense - Other Co": frappe._dict(
		company="Other Co", root_type="Expense", is_group=0, disabled=0, account_type=""
	),
}


def _account_details(doctype, name, fieldname=None, *args, **kwargs):
	if doctype == "Account":
		return ACCOUNTS.get(name)
	raise AssertionError(f"unexpected read: {doctype} {name}")


def _doc():
	return frappe._dict(
		doctype="Delivery Note", name="DN-1", company=COMPANY, posting_date="2026-09-24"
	)


class TestPostingRevalidatesTheAccounts(unittest.TestCase):
	"""§6.3: configuration can change after it was saved, so posting checks it again."""

	def setUp(self):
		self.new_doc = MagicMock()
		for p in (
			patch(
				f"{settings_module.__name__}.frappe.db.get_value",
				side_effect=_account_details,
			),
			patch.object(cgf.frappe, "new_doc", self.new_doc),
		):
			p.start()
			self.addCleanup(p.stop)

	def _post(self, liability, adjustment):
		accounts = frappe._dict(
			liability_account=liability, cogs_adjustment_account=adjustment
		)
		return cgf._build_settlement_entry(_doc(), accounts, {"CUST": 100.0}, 100.0, 2)

	def test_a_liability_to_liability_mapping_is_refused_before_any_entry_exists(self):
		"""The KGJPL mapping. Blocked at posting even though settings save never saw it."""
		with self.assertRaises(frappe.ValidationError) as raised:
			self._post(LIABILITY, ADVANCES)
		self.assertIn("would move the obligation", str(raised.exception))
		self.assertIn("Delivery Note DN-1", str(raised.exception))
		self.new_doc.assert_not_called()

	def test_the_same_account_on_both_legs_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as raised:
			self._post(LIABILITY, LIABILITY)
		self.assertIn("are both set to", str(raised.exception))
		self.new_doc.assert_not_called()

	def test_a_missing_adjustment_account_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as raised:
			self._post(LIABILITY, None)
		self.assertIn("is mandatory", str(raised.exception))
		self.new_doc.assert_not_called()

	def test_every_refusal_at_posting_says_where_to_correct_the_row(self):
		"""The operator on a Delivery Note cannot see the settings row the message is about.

		DN-26-00025 stopped with a message that named the account but not where to change it.
		"""
		for liability, adjustment, reason in (
			(LIABILITY, None, "is mandatory"),
			(LIABILITY, "No Such Account - CG", "does not exist"),
			(LIABILITY, "Group Expense - CG", "is a group account"),
			(LIABILITY, "Disabled Expense - CG", "is disabled"),
			(LIABILITY, "Payable - CG", "requires a Party"),
			(LIABILITY, "Stock In Hand - CG", "is a Stock account"),
			(LIABILITY, "Expense - Other Co", "belongs to Company"),
			(ADJUSTMENT, ADJUSTMENT, "must be of root type"),
			(LIABILITY, LIABILITY, "are both set to"),
			(LIABILITY, ADVANCES, "would move the obligation"),
		):
			with self.subTest(reason=reason):
				with self.assertRaises(frappe.ValidationError) as raised:
					self._post(liability, adjustment)
				message = str(raised.exception)
				self.assertIn(reason, message)
				self.assertIn("Company Accounts, on the row for Company", message)
				self.assertIn(frappe.bold(COMPANY), message)
				self.new_doc.assert_not_called()


class TestSettlementLinesCostCenter(unittest.TestCase):
	"""A Profit and Loss line without a cost center is refused by erpnext ("Missing Cost Center").

	Both lines take the cost center Frappe's defaults give the submitting user, as they always did.
	When those defaults give none -- Frappe drops ``:Company`` for a user whose Cost Center user
	permissions exclude the company's -- they fall back to the company's cost center.
	"""

	COMPANY_COST_CENTER = "Main - CG"
	USER_COST_CENTER = "Branch - CG"

	def setUp(self):
		self.entry = MagicMock()
		self.row_defaults = {}
		self.new_doc = MagicMock(
			side_effect=lambda doctype, **kwargs: dict(self.row_defaults)
			if doctype == "Journal Entry Account"
			else self.entry
		)
		real_get_cached_value = frappe.get_cached_value

		def cached_value(doctype, *args, **kwargs):
			# Answer only the two exact reads this path makes; everything else reads the site.
			if doctype == "Company" and args[:2] == (COMPANY, "cost_center"):
				return self.COMPANY_COST_CENTER
			if doctype == "Account" and args[:2] == (LIABILITY, "account_type"):
				return ""
			return real_get_cached_value(doctype, *args, **kwargs)

		for p in (
			patch.object(cgf, "validate_settlement_accounts"),
			patch.object(cgf.frappe, "new_doc", self.new_doc),
			patch.object(cgf.frappe, "get_cached_value", side_effect=cached_value),
		):
			p.start()
			self.addCleanup(p.stop)

	def _lines(self, per_customer, total):
		accounts = frappe._dict(
			liability_account=LIABILITY, cogs_adjustment_account=ADJUSTMENT
		)
		cgf._build_settlement_entry(_doc(), accounts, per_customer, total, 2)
		return [
			c.args[1]
			for c in self.entry.append.call_args_list
			if c.args[0] == "accounts"
		]

	def test_with_no_default_from_the_user_both_legs_take_the_company_cost_center(self):
		lines = self._lines({"CUST": 100.0}, 100.0)
		self.assertEqual([line["account"] for line in lines], [LIABILITY, ADJUSTMENT])
		self.assertEqual(
			{line["cost_center"] for line in lines}, {self.COMPANY_COST_CENTER}
		)

	def test_a_cost_center_from_the_users_defaults_is_kept(self):
		"""Nothing changes for a user whose defaults already give a cost center."""
		self.row_defaults = {"cost_center": self.USER_COST_CENTER}
		lines = self._lines({"CUST": 100.0}, 100.0)
		self.assertEqual(
			{line["cost_center"] for line in lines}, {self.USER_COST_CENTER}
		)

	def test_the_defaults_are_those_of_this_entrys_rows(self):
		"""``:Company`` resolves through the parent, so the entry must be passed as parent_doc."""
		self._lines({"CUST": 100.0}, 100.0)
		self.new_doc.assert_any_call(
			"Journal Entry Account",
			parent_doc=self.entry,
			parentfield="accounts",
			as_dict=True,
		)
		self.assertEqual(self.entry.company, COMPANY)

	def test_both_legs_of_a_return_carry_the_same_cost_center(self):
		"""A physical return inverts both legs; the cost center must not depend on direction."""
		lines = self._lines({"CUST": -40.0}, -40.0)
		self.assertEqual(lines[0]["credit_in_account_currency"], 40.0)
		self.assertEqual(lines[1]["debit_in_account_currency"], 40.0)
		self.assertEqual(
			{line["cost_center"] for line in lines}, {self.COMPANY_COST_CENTER}
		)


class TestSettlementClaimsOnce(unittest.TestCase):
	"""§6.5: a retry, or a concurrent submit, must not post a second JE for the same events."""

	def setUp(self):
		# Answer only for the ledger. Frappe's own metadata loading goes through the same
		# ``get_values`` and must keep reading the real database.
		real_get_values = frappe.db.get_values
		self.events = []
		self.get_values = MagicMock(
			side_effect=lambda doctype, *args, **kwargs: list(self.events)
			if doctype == cgf.LEDGER_DOCTYPE
			else real_get_values(doctype, *args, **kwargs)
		)
		self.set_value = MagicMock()
		self.build = MagicMock(return_value="JE-1")
		for p in (
			patch.object(
				cgf,
				"get_customer_gold_valuation_policy",
				return_value=VALUATION_NOMINAL,
			),
			patch.object(
				cgf,
				"get_customer_gold_company_settings",
				return_value=frappe._dict(
					liability_account=LIABILITY, cogs_adjustment_account=ADJUSTMENT
				),
			),
			patch.object(cgf, "_build_settlement_entry", self.build),
			patch(f"{cgf.__name__}.frappe.db.get_values", self.get_values),
			patch(f"{cgf.__name__}.frappe.db.set_value", self.set_value),
			patch(f"{cgf.__name__}.frappe.get_precision", return_value=2),
		):
			p.start()
			self.addCleanup(p.stop)

	@staticmethod
	def _event(name, value):
		return frappe._dict(
			name=name,
			customer="CUST",
			cg_carrying_value_delta=value,
			cg_source_row="ROW-1",
			serial_no="S-1",
			batch_no="FG-1",
		)

	def test_the_unclaimed_events_are_read_with_a_lock(self):
		"""A plain read would let two submits both see the events unclaimed and both post."""
		self.events = [self._event("EV-1", -100.0)]
		cgf.settle_customer_gold_liability(_doc(), ["EV-1"])
		(ledger_read,) = [
			c for c in self.get_values.call_args_list if c.args[0] == cgf.LEDGER_DOCTYPE
		]
		self.assertTrue(ledger_read.kwargs.get("for_update"))

	def test_a_retry_that_finds_every_event_claimed_posts_nothing(self):
		self.events = []
		self.assertIsNone(cgf.settle_customer_gold_liability(_doc(), ["EV-1"]))
		self.build.assert_not_called()

	def test_only_the_events_the_entry_settled_are_claimed(self):
		"""An unvalued event (stored as 0) stays claimable, so a later correction can settle it."""
		self.events = [
			self._event("EV-1", -100.0),
			self._event("EV-2", 0),
		]
		cgf.settle_customer_gold_liability(_doc(), ["EV-1", "EV-2"])
		claimed = [c.args[1] for c in self.set_value.call_args_list]
		self.assertEqual(claimed, ["EV-1"])

	def test_the_entry_is_told_which_events_it_settles(self):
		"""§6.4: the JE's remark lists them; the structural link is cg_settlement_voucher."""
		self.events = [
			self._event("EV-1", -100.0),
			self._event("EV-2", 0),
		]
		cgf.settle_customer_gold_liability(_doc(), ["EV-1", "EV-2"])
		events = self.build.call_args.args[5]
		self.assertEqual([e.name for e in events], ["EV-1"])
