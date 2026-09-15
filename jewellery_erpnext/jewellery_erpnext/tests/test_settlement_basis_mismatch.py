# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""Repack settlement mixes FINE and REFERENCE gram bases.

NOT in the specification's C01-C15 -- found while auditing this programme's own
documentation. Nothing here is fixed; these cases exist to pin the arithmetic and to make
the interaction below impossible to rediscover by accident.

THE TWO BASES
-------------
    fine grams      = gross x item_purity / 100
    reference grams = gross x item_purity / reference_purity

``Stock Entry Detail.custom_pure_qty`` holds REFERENCE grams: ``doc_events/stock_entry.py``
computes ``item_purity * qty / pure_item_purity`` where ``pure_item_purity`` is the purity
of the Manufacturing Setting's ``pure_gold_item``. (``sub_utils/snc.py`` computes a FINE
value into the same key, but only in memory -- ``before_validate`` overwrites it on every
save -- so the stored column is consistently reference basis.)

THE DEFECT
----------
``sub_utils/repack.py`` ``_settle_pending_logs``::

    available_pure_qty = flt(incoming["remaining_qty"] * source_purity / 100, 3)   # FINE
    consume_pure_qty   = min(required_pure_qty, available_pure_qty)                # mixes
    consume_gross_qty  = flt(consume_pure_qty * 100 / source_purity, 3)            # FINE inverse

``required_pure_qty`` derives from ``Subcontracting Log.balance_pure_qty``, which is copied
from ``custom_pure_qty`` -- REFERENCE basis. So a fine quantity is compared against a
reference one, and the winner is converted back with a fine inverse. ``consume_gross_qty``
becomes the physical quantity on a submitted Stock Entry.

WHY NOBODY HAS SEEN IT
----------------------
On this bench the two bases coincide, because ``pure_item_purity`` resolves to 100.0 -- and
it resolves to 100.0 only because the ``Attribute Value`` row named ``99.9`` wrongly carries
``purity_percentage = 100.0``.

**Repairing that master would EXPOSE this bug, not fix a 0.1% rounding.** The two changes
must be sequenced together. That is the single most important thing recorded in this file.
"""

import unittest

from frappe.tests import IntegrationTestCase
from frappe.utils import flt


def _fine_grams(gross, purity):
	return flt(gross * purity / 100, 3)


def _reference_grams(gross, purity, reference_purity):
	return flt(gross * purity / reference_purity, 3)


def _settlement_gross(required_pure_qty, remaining_qty, source_purity):
	"""The arithmetic of ``repack._settle_pending_logs``, reproduced exactly."""
	available_pure_qty = flt(remaining_qty * source_purity / 100, 3)
	consume_pure_qty = min(required_pure_qty, available_pure_qty)
	return flt(consume_pure_qty * 100 / source_purity, 3)


class TestSettlementBasisMismatch(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_the_two_bases_differ_at_a_realistic_reference(self):
		"""100 g of 75.4% metal: 75.400 fine grams, 75.475 reference grams."""
		self.assertEqual(_fine_grams(100, 75.4), 75.400)
		self.assertEqual(_reference_grams(100, 75.4, 99.9), 75.475)

	def test_the_two_bases_coincide_only_at_a_100_reference(self):
		"""Why the defect is invisible here: gk's reference purity resolves to 100.0."""
		self.assertEqual(_reference_grams(100, 75.4, 100.0), _fine_grams(100, 75.4))

	def test_settlement_is_correct_while_the_reference_is_100(self):
		"""Today's behaviour. A 75.4% source settling a 75.400-reference obligation.

		With a 100.0 reference the obligation IS the fine quantity, so the fine-basis
		arithmetic happens to be right and exactly 100 g is consumed.
		"""
		required = _reference_grams(100, 75.4, 100.0)
		self.assertEqual(_settlement_gross(required, 500, 75.4), 100.0)

	@unittest.expectedFailure
	def test_settlement_is_wrong_once_the_reference_master_is_repaired(self):
		"""THE DEFECT. Marked expectedFailure: it documents a real bug, still unfixed.

		Repair the ``99.9`` Attribute Value and the obligation becomes 75.475 REFERENCE
		grams for the same physical metal. ``_settle_pending_logs`` then treats 75.475 as
		if it were fine grams and consumes ``75.475 * 100 / 75.4 = 100.100`` gross grams --
		100.1 g of physical metal to discharge an obligation that 100 g covers.

		When this test starts PASSING, the units bug has been fixed and the marker should
		be removed.
		"""
		required = _reference_grams(100, 75.4, 99.9)
		self.assertEqual(
			_settlement_gross(required, 500, 75.4),
			100.0,
			"settlement consumed the wrong physical quantity",
		)

	def test_the_size_of_the_error_is_quantified(self):
		"""Pins the magnitude so the expectedFailure above cannot be dismissed as rounding."""
		required = _reference_grams(100, 75.4, 99.9)
		consumed = _settlement_gross(required, 500, 75.4)
		# 100.099, not 100.100 -- the intermediate is rounded to 3 dp twice, exactly as
		# production does it. Asserted as the code actually computes it.
		self.assertEqual(consumed, 100.099)
		self.assertAlmostEqual((consumed - 100.0) / 100.0 * 100, 0.099, places=3)

	def test_the_error_scales_with_quantity(self):
		"""0.1% of a 10 kg settlement is 10 g of gold, not a rounding artefact."""
		required = _reference_grams(10_000, 75.4, 99.9)
		consumed = _settlement_gross(required, 50_000, 75.4)
		self.assertAlmostEqual(consumed - 10_000.0, 10.0, places=1)
