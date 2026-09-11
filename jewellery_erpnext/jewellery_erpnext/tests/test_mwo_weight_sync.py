# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for ManufacturingWorkOrder.sync_mwo_weights gross_wt re-derivation.

The FG MWO's ``gross_wt`` must be re-derived from the corrected components
(``net + finding + carat_to_gram(diamond) + carat_to_gram(gemstone) + other``)
after the sibling SUM, never carried as the raw sum-of-rounded sibling headers.

Regression: MWO-KGJPL-NE05395-003-1-01 stored ``gross_wt`` 31.200 because the
SUM folded each sibling's independently-rounded ``diamond_wt_in_gram`` (0.501 +
0.351 = 0.852) while a single conversion of the summed carats (4.256 x 0.2 =
0.851) is the value the Serial Number Creator and ``update_wt_detail`` use --
so the MWO disagreed with its own buckets (31.199) and with the SNC it feeds.
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order import (
	ManufacturingWorkOrder,
)
from jewellery_erpnext.utils import carat_to_gram


class _SetValueHarness:
	"""Capture frappe.db.set_value calls so tests can assert the buckets written."""

	def __init__(self):
		self.calls = []

	def __call__(self, doctype, name, fields=None, *args, **kwargs):
		self.calls.append((doctype, name, fields))


class TestSyncMwoWeightsGrossWtRederivation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, agg_row):
		doc = SimpleNamespace(
			name="MWO-FG-TEST",
			manufacturing_order="PMO-TEST",
			manufacturing_operation="MOP-FG-TEST",
		)
		harness = _SetValueHarness()

		def fake_get_all(
			doctype, filters=None, fields=None, pluck=None, order_by=None, **kwargs
		):
			if doctype == "Manufacturing Work Order":
				return ["MWO-SIB-1", "MWO-SIB-2"]
			if doctype == "Manufacturing Operation":
				# Latest MOP per sibling MWO, creation desc (one each).
				return [
					frappe._dict(name="MOP-1", manufacturing_work_order="MWO-SIB-1"),
					frappe._dict(name="MOP-2", manufacturing_work_order="MWO-SIB-2"),
				]
			return []

		# flt(value, precision) -> rounded() -> frappe.get_system_settings("rounding_method"),
		# a real DB/cache read. On the disposable test_site that lookup raises and flt
		# swallows the exception into 0.0 (carat_to_gram(4.256) became 0.0). Pin a valid
		# method so the round is deterministic -- every valid method agrees at 3 dp for
		# these assertions.
		with patch.object(frappe.db, "get_all", side_effect=fake_get_all), patch.object(
			frappe.db, "sql", return_value=[agg_row]
		), patch.object(frappe.db, "set_value", side_effect=harness), patch.object(
			frappe, "get_system_settings", return_value="Banker's Rounding (legacy)"
		):
			ManufacturingWorkOrder.sync_mwo_weights(doc)

		writes = {}
		for doctype, name, fields in harness.calls:
			if fields is None:
				continue
			writes[(doctype, name)] = fields
		return doc.gross_wt, writes

	def _agg_row(self, **overrides):
		row = {
			"gross_wt": 0.0,
			"net_wt": 0.0,
			"finding_wt": 0.0,
			"diamond_wt": 0.0,
			"gemstone_wt": 0.0,
			"other_wt": 0.0,
			"received_gross_wt": 0.0,
			"received_net_wt": 0.0,
			"loss_wt": 0.0,
			"diamond_pcs": 0.0,
			"gemstone_pcs": 0.0,
		}
		row.update(overrides)
		return row

	def _mwo_write(self, writes):
		return writes[("Manufacturing Work Order", "MWO-FG-TEST")]

	def _mop_write(self, writes):
		return writes[("Manufacturing Operation", "MOP-FG-TEST")]

	def test_gross_wt_rederived_from_corrected_buckets(self):
		"""The drifted sibling SUM must not leak into gross_wt.

		Siblings each converted their own diamond carats (0.501 + 0.351 = 0.852),
		so the SUM(gross_wt) = 31.200; a single conversion of the summed carats is
		0.851, giving net 26.798 + finding 3.550 + 0.851 = 31.199.
		"""
		agg = self._agg_row(
			gross_wt=31.200,
			net_wt=26.798,
			finding_wt=3.550,
			diamond_wt=4.256,
		)

		gross, writes = self._run(agg)

		# gross_wt is re-derived to the SNC round-of-sum, not the SUM.
		self.assertAlmostEqual(gross, 31.199, places=3)
		mwo = self._mwo_write(writes)
		# The gram twin is derived once from the summed carats.
		self.assertAlmostEqual(mwo["diamond_wt_in_gram"], 0.851, places=3)
		# The FG MOP receives the same corrected value.
		mop = self._mop_write(writes)
		self.assertAlmostEqual(mop["gross_wt"], 31.199, places=3)

	def test_gross_wt_round_of_sum_for_all_families(self):
		"""gross_wt equals net + finding + per-family carat conversions + other."""
		agg = self._agg_row(
			gross_wt=2.900,  # stale sibling SUM; must be ignored
			net_wt=2.273,
			finding_wt=2.334,
			diamond_wt=1.0,
			gemstone_wt=2.0,
			other_wt=0.5,
		)

		gross, writes = self._run(agg)

		# Same pinned rounding as _run, so the reference computation is not a DB read.
		with patch.object(
			frappe, "get_system_settings", return_value="Banker's Rounding (legacy)"
		):
			expected = flt(
				2.273 + 2.334 + 0.5 + carat_to_gram(1.0) + carat_to_gram(2.0), 3
			)
		self.assertAlmostEqual(gross, expected, places=3)
		self.assertAlmostEqual(
			self._mop_write(writes)["gross_wt"],
			expected,
			places=3,
		)
