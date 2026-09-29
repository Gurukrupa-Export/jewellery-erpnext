# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""F4 -- the settlement Journal Entry revalidates its accounts, and posts once.

``KGJPL-JE-JE-26-00018`` posted Dr Customer Goods Receive / Cr Advances from Customers: two
liability accounts. Nothing was discharged. The KGJPL settings row had never been validated,
because save-time validation returns early while the feature flag is off. So the rules now run
again at posting time, before anything is inserted.

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
