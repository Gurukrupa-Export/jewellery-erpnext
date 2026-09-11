# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for the weight backfill patches.

``patches.fix_mwo_gross_weight`` and ``patches.fix_bom_gross_weight`` run
automatically via ``bench migrate`` on production, so their detect/review
contract is worth pinning: they repair only rounding-sized drift, judge each
surface independently (a canonical gross does not block repairing a stale gram
twin / FG MOP / Serial No mirror), prefer the linked Manufacturing Operation,
and send anything beyond ``ROUNDING_DRIFT_CEILING`` to review.

Mock-based per the house pattern: no DB fixtures, every DB call patched.
"""

from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.patches import fix_bom_gross_weight, fix_mwo_gross_weight

ROUNDING = "Banker's Rounding (legacy)"

MWO_FIELDS = [
	"name",
	"net_wt",
	"finding_wt",
	"diamond_wt",
	"gemstone_wt",
	"other_wt",
	"diamond_wt_in_gram",
	"gross_wt",
	"manufacturing_operation",
]


def make_mwo(gross=31.199, diamond_gram=0.851, linked_mop=None):
	return frappe._dict(
		{
			"name": "MWO-1",
			"net_wt": 26.798,
			"finding_wt": 3.550,
			"diamond_wt": 4.256,
			"gemstone_wt": 0,
			"other_wt": 0,
			"diamond_wt_in_gram": diamond_gram,
			"gross_wt": gross,
			"manufacturing_operation": linked_mop,
		}
	)


def make_mop(gross=31.199, diamond_gram=0.851, gemstone_gram=0):
	return frappe._dict(
		{
			"gross_wt": gross,
			"diamond_wt_in_gram": diamond_gram,
			"gemstone_wt_in_gram": gemstone_gram,
		}
	)


class TestMwoBackfill(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run_detect(self, mwo, mop=None):
		"""Detect over one FG MWO, with an optional FG MOP served by get_value.

		``_detect`` queries FG MWOs via the module-level ``frappe.get_all`` (not
		``frappe.db.get_all``) and attribute-accesses the rows, so the mock must
		patch ``frappe.get_all`` and hand back a ``frappe._dict``.
		"""
		mop = mop or {}
		real_get_all = frappe.get_all
		real_get_value = frappe.db.get_value

		def get_all(doctype, filters=None, fields=None, **kwargs):
			if doctype == "Manufacturing Work Order":
				return [mwo]
			return real_get_all(doctype, filters=filters, fields=fields, **kwargs)

		def get_value(
			doctype, filters, fieldname, order_by=None, as_dict=None, **kwargs
		):
			if doctype != "Manufacturing Operation":
				return real_get_value(
					doctype,
					filters,
					fieldname,
					order_by=order_by,
					as_dict=as_dict,
					**kwargs,
				)
			if isinstance(fieldname, str):  # fallback latest-MOP lookup
				return mop.get("name")
			return {
				"gross_wt": mop.get("gross_wt"),
				"diamond_wt_in_gram": mop.get("diamond_wt_in_gram"),
				"gemstone_wt_in_gram": mop.get("gemstone_wt_in_gram"),
			}

		with patch.object(
			frappe, "get_system_settings", return_value=ROUNDING
		), patch.object(
			fix_mwo_gross_weight, "_mwo_pages", Mock(return_value=[[mwo["name"]]])
		), patch.object(frappe, "get_all", get_all), patch.object(
			frappe.db, "get_value", get_value
		):
			return fix_mwo_gross_weight._detect()

	def test_plus_0_001_rounding_drift_is_repaired(self):
		corrections, review = self._run_detect(make_mwo(gross=31.200))
		self.assertEqual(len(corrections), 1, corrections)
		self.assertIn("gross_wt", corrections[0]["mwo_issues"])
		self.assertEqual(corrections[0]["corrections_map"]["gross_wt"], 31.199)
		self.assertEqual(review, [])

	def test_minus_0_001_rounding_drift_is_repaired(self):
		corrections, review = self._run_detect(make_mwo(gross=31.198))
		self.assertEqual(len(corrections), 1, corrections)
		self.assertIn("gross_wt", corrections[0]["mwo_issues"])
		self.assertEqual(corrections[0]["corrections_map"]["gross_wt"], 31.199)
		self.assertEqual(review, [])

	def test_large_discrepancy_goes_to_review(self):
		corrections, review = self._run_detect(make_mwo(gross=40.000))
		self.assertEqual(corrections, [])
		self.assertEqual(len(review), 1)
		self.assertIn("exceeds the", review[0]["reason"])

	def test_canonical_gross_but_stale_gram_twin_is_repaired(self):
		"""gross canonical but diamond_wt_in_gram stale (0.852) -> still repaired."""
		corrections, review = self._run_detect(
			make_mwo(gross=31.199, diamond_gram=0.852)
		)
		self.assertEqual(len(corrections), 1, corrections)
		self.assertIn("diamond_wt_in_gram", corrections[0]["mwo_issues"])
		self.assertEqual(corrections[0]["corrections_map"]["diamond_wt_in_gram"], 0.851)
		self.assertEqual(review, [])

	def test_canonical_gross_but_stale_mop_is_repaired(self):
		"""MWO gross canonical but linked FG MOP gross stale -> MOP repaired."""
		mwo = make_mwo(gross=31.199, linked_mop="MOP-X")
		corrections, review = self._run_detect(mwo, make_mop(gross=31.200))
		self.assertEqual(len(corrections), 1, corrections)
		self.assertEqual(corrections[0]["mop"], "MOP-X")
		self.assertEqual(corrections[0]["mwo_issues"], {})
		self.assertIn("gross_wt", corrections[0]["mop_issues"])

	def test_linked_mop_preferred_over_fallback(self):
		"""Fallback returns MOP-FALLBACK but linked MOP-X must govern."""
		mwo = make_mwo(gross=31.200, linked_mop="MOP-X")
		corrections, review = self._run_detect(mwo, make_mop(gross=31.199))
		self.assertEqual(len(corrections), 1, corrections)
		self.assertEqual(corrections[0]["mop"], "MOP-X")

	def test_no_mop_exists_is_safe(self):
		"""No linked MOP and no fallback -> only the MWO itself is repaired."""
		corrections, review = self._run_detect(make_mwo(gross=31.200))
		self.assertEqual(len(corrections), 1, corrections)
		self.assertIsNone(corrections[0]["mop"])
		self.assertEqual(corrections[0]["mop_issues"], {})
		self.assertEqual(review, [])

	def test_idempotent_second_detect_finds_nothing(self):
		"""After the corrections are applied, a second pass finds nothing."""
		mwo = make_mwo(gross=31.200)
		corrections, review = self._run_detect(mwo)
		self.assertEqual(len(corrections), 1)
		mwo.update(corrections[0]["corrections_map"])
		again, review2 = self._run_detect(mwo)
		self.assertEqual(again, [])
		self.assertEqual(review2, [])

	def test_dry_run_writes_nothing(self):
		corrections, review = self._run_detect(make_mwo(gross=31.200))
		with patch.object(
			fix_mwo_gross_weight,
			"_detect",
			return_value=(corrections, review),
		), patch.object(
			frappe.db,
			"set_value",
			wraps=lambda *a, **k: (_ for _ in ()).throw(AssertionError("set_value")),
		), patch.object(frappe.db, "commit") as commit, patch.object(
			frappe, "get_system_settings", return_value=ROUNDING
		):
			fix_mwo_gross_weight.execute(dry_run=True)
		commit.assert_not_called()


class TestBomBackfill(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	BOM = {
		"name": "BOM-1",
		"diamond_weight": 4.256,
		"gemstone_weight": 0,
		"metal_weight": 26.798,
		"finding_weight_": 3.550,
		"other_weight": 0,
		"total_diamond_weight_in_gms": 0.851,
		"total_gemstone_weight_in_gms": 0,
		"gross_weight": 31.199,
	}

	def _run_detect(self, bom=None, serial_gross=None, serial_names=None):
		"""Detect over one BOM + linked Serial Nos.

		``_detect`` queries BOMs via module-level ``frappe.get_all`` and stale
		serials via ``frappe.db.get_all``, attribute-accesses both -- mock rows
		must be ``frappe._dict`` and both entry points patched.
		"""
		bom = frappe._dict(bom or dict(self.BOM))
		serial_names = serial_names or ["SN-1"]
		serial_gross = bom["gross_weight"] if serial_gross is None else serial_gross
		page = [
			frappe._dict({"name": name, "custom_bom_no": "BOM-1"})
			for name in serial_names
		]
		stale = (
			[]
			if serial_gross == bom["gross_weight"]
			else [frappe._dict({"name": name}) for name in serial_names]
		)
		real_get_all = frappe.get_all

		def get_all(doctype, filters=None, fields=None, **kwargs):
			if doctype == "BOM":
				return [bom]
			if doctype == "Serial No":
				return stale
			return real_get_all(doctype, filters=filters, fields=fields, **kwargs)

		with patch.object(
			frappe, "get_system_settings", return_value=ROUNDING
		), patch.object(
			fix_bom_gross_weight, "_bom_serial_pages", Mock(return_value=[page])
		), patch.object(frappe, "get_all", get_all), patch.object(
			frappe.db, "get_all", get_all
		):
			return fix_bom_gross_weight._detect()

	def test_gram_twins_and_gross_repaired(self):
		bom = dict(self.BOM)
		bom["total_diamond_weight_in_gms"] = 0.8512
		bom["gross_weight"] = 31.1992
		bom_corrections, serial_corrections, review = self._run_detect(bom)
		self.assertEqual(len(bom_corrections), 1)
		self.assertEqual(
			bom_corrections[0]["corrections_map"]["total_diamond_weight_in_gms"], 0.851
		)
		self.assertEqual(bom_corrections[0]["corrections_map"]["gross_weight"], 31.199)
		self.assertEqual(review, [])

	def test_serial_mirrors_corrected_gross(self):
		bom = dict(self.BOM)
		bom["total_diamond_weight_in_gms"] = 0.8512
		bom["gross_weight"] = 31.1992
		bom_corrections, serial_corrections, review = self._run_detect(
			bom, serial_gross=31.200
		)
		self.assertEqual(serial_corrections, {"SN-1": 31.199})

	def test_bom_canonical_but_serial_stale_is_repaired(self):
		bom_corrections, serial_corrections, review = self._run_detect(
			dict(self.BOM), serial_gross=31.200
		)
		self.assertEqual(bom_corrections, [])
		self.assertEqual(serial_corrections, {"SN-1": 31.199})

	def test_large_discrepancy_goes_to_review(self):
		bom = dict(self.BOM)
		bom["gross_weight"] = 40.000
		bom_corrections, serial_corrections, review = self._run_detect(
			bom, serial_gross=40.000
		)
		self.assertEqual(bom_corrections, [])
		self.assertEqual(serial_corrections, {})
		# The BOM and its linked Serial No BOTH go to review (2 entries).
		self.assertEqual(len(review), 2, review)
		self.assertIn("exceeds the", review[0]["reason"])
		self.assertIn("serial left for review", review[1]["reason"])

	def test_multiple_serials_reference_one_bom(self):
		"""Every Serial No linked to a repaired BOM is mirrored."""
		bom = dict(self.BOM)
		bom["total_diamond_weight_in_gms"] = 0.8512
		bom["gross_weight"] = 31.1992
		bom_corrections, serial_corrections, review = self._run_detect(
			bom, serial_gross=31.200, serial_names=["SN-1", "SN-2", "SN-3"]
		)
		self.assertEqual(len(bom_corrections), 1)
		self.assertEqual(
			serial_corrections, {"SN-1": 31.199, "SN-2": 31.199, "SN-3": 31.199}
		)
		self.assertEqual(review, [])

	def test_idempotent_second_detect_finds_nothing(self):
		"""After the corrections are applied, a second pass finds nothing."""
		bom = dict(self.BOM)
		bom["total_diamond_weight_in_gms"] = 0.8512
		bom["gross_weight"] = 31.1992
		bom_corrections, serial_corrections, review = self._run_detect(bom)
		self.assertEqual(len(bom_corrections), 1)
		bom.update(bom_corrections[0]["corrections_map"])
		again, serials2, review2 = self._run_detect(bom)
		self.assertEqual(again, [])
		self.assertEqual(serials2, {})
		self.assertEqual(review2, [])

	def test_dry_run_writes_nothing(self):
		bom = dict(self.BOM)
		bom["gross_weight"] = 31.1992
		bom_corrections, serial_corrections, review = self._run_detect(bom)
		self.assertEqual(len(bom_corrections), 1)
		with patch.object(
			fix_bom_gross_weight,
			"_detect",
			return_value=(bom_corrections, serial_corrections, review),
		), patch.object(
			frappe.db,
			"set_value",
			wraps=lambda *a, **k: (_ for _ in ()).throw(AssertionError("set_value")),
		), patch.object(frappe.db, "commit") as commit, patch.object(
			frappe, "get_system_settings", return_value=ROUNDING
		):
			fix_bom_gross_weight.execute(dry_run=True)
		commit.assert_not_called()

	def test_missing_referenced_bom_handled_safely(self):
		"""A Serial No whose BOM no longer exists must not break detect."""
		bom = dict(self.BOM)
		bom["gross_weight"] = 31.1992
		page = [
			frappe._dict({"name": "SN-LIVE", "custom_bom_no": "BOM-1"}),
			frappe._dict({"name": "SN-GONE", "custom_bom_no": "BOM-GONE"}),
		]
		real_get_all = frappe.get_all

		def get_all(doctype, filters=None, fields=None, **kwargs):
			if doctype == "BOM":
				names = (filters or {}).get("name")
				in_list = (
					names[1] if isinstance(names, tuple) and len(names) > 1 else []
				)
				return [frappe._dict(bom)] if "BOM-1" in in_list else []
			if doctype == "Serial No":
				return [frappe._dict({"name": "SN-LIVE"})]
			return real_get_all(doctype, filters=filters, fields=fields, **kwargs)

		with patch.object(
			frappe, "get_system_settings", return_value=ROUNDING
		), patch.object(
			fix_bom_gross_weight, "_bom_serial_pages", Mock(return_value=[page])
		), patch.object(frappe, "get_all", get_all), patch.object(
			frappe.db, "get_all", get_all
		):
			bom_corrections, serial_corrections, review = fix_bom_gross_weight._detect()

		# BOM-GONE simply does not appear in the results; the live one is repaired.
		self.assertEqual([c["bom"] for c in bom_corrections], ["BOM-1"])
		self.assertEqual(serial_corrections, {"SN-LIVE": 31.199})
		self.assertEqual(review, [])
