# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Company prefix resolution for variant batch names.

Batch creation is implicit -- a Stock Entry mints one on submit -- so the prefix
resolver must never be the reason a stock transaction aborts. The group companies keep
their hand-picked codes (GE/KG/SD/SHC, baked into every existing batch name); anything
else falls back to the Company's own ``abbr``, and only a batch whose company cannot be
determined at all on a multi-company site is refused.

DB-free per the suite convention: ``setUpClass`` is neutralized and the resolver runs
against ``frappe._dict`` docs with ``frappe.db`` / ``frappe.defaults`` mocked.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.customization.batch import (
	batch as batch_module,
)


def _doc(**fields):
	defaults = {"item": "D-NT-RO-6B-+9-9.5"}
	defaults.update(fields)
	return frappe._dict(defaults)


class _Ctx:
	"""Patch the three sources the resolver reads: user default, global default and
	the Company table (``abbr`` lookup + the single-company probe)."""

	def __init__(self, user_default=None, global_default=None, companies=(), abbr=None):
		self.patches = [
			patch.object(
				frappe.defaults, "get_user_default", return_value=user_default
			),
			patch.object(
				frappe.defaults, "get_global_default", return_value=global_default
			),
			patch.object(frappe, "get_all", return_value=list(companies)),
			patch.object(frappe.db, "get_value", return_value=abbr),
		]

	def __enter__(self):
		for p in self.patches:
			p.start()
		return self

	def __exit__(self, *exc):
		for p in self.patches:
			p.stop()


class TestBatchCompanyAbbr(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_group_company_keeps_its_hand_picked_code(self):
		"""The four Gurukrupa companies must never drift to Company.abbr -- 20k+
		existing batch names carry these prefixes."""
		with _Ctx(abbr="GEPL"):
			self.assertEqual(
				batch_module.get_batch_company_abbr(
					_doc(custom_company="Gurukrupa Export Private Limited")
				),
				"GE",
			)
			self.assertEqual(
				batch_module.get_batch_company_abbr(
					_doc(custom_company="Sadguru Hallmarking Centre")
				),
				"SHC",
			)

	def test_batch_company_wins_over_session_default(self):
		"""A caller that knows which company owns the material (Refining Entry sets
		custom_company) must not be overridden by whoever is logged in."""
		with _Ctx(user_default="Gurukrupa Export Private Limited"):
			self.assertEqual(
				batch_module.get_batch_company_abbr(
					_doc(custom_company="KG GK Jewellers Private Limited")
				),
				"KG",
			)

	def test_unmapped_company_falls_back_to_company_abbr(self):
		"""A company nobody added to the map still mints batches -- the alternative is
		aborting the Stock Entry that created it."""
		with _Ctx(abbr="TC"):
			self.assertEqual(
				batch_module.get_batch_company_abbr(
					_doc(custom_company="Test_Company")
				),
				"TC",
			)

	def test_company_abbr_is_sanitized(self):
		"""Batch names are '-' delimited (batch_rename reads the trailing serial), so a
		prefix can never carry a separator."""
		with _Ctx(abbr="T-C 1"):
			self.assertEqual(
				batch_module.get_batch_company_abbr(
					_doc(custom_company="Test_Company")
				),
				"TC1",
			)

	def test_single_company_site_needs_no_default(self):
		"""A fresh bench / CI site has no default company anywhere; with exactly one
		Company there is nothing to disambiguate."""
		with _Ctx(companies=["Test_Company"], abbr="T"):
			self.assertEqual(batch_module.get_batch_company_abbr(_doc()), "T")

	def test_ambiguous_multi_company_site_is_refused(self):
		"""No company anywhere AND several to choose from: guessing would stamp one
		company's material with another's code, which is unrecoverable once the batch
		is transacted."""
		with _Ctx(companies=["Company A", "Company B"]):
			with self.assertRaises(frappe.ValidationError):
				batch_module.get_batch_company_abbr(_doc())
