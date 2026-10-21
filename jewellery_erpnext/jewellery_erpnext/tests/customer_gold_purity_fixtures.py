# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""Isolated purity fixtures for the Customer Gold work.

WHY THIS EXISTS
---------------
Purity reaches ``custom_pure_qty`` through a three-hop join that ends at a **single
bench-wide master row**::

	Item -> Item Variant Attribute (attribute="Metal Purity") -> Attribute Value.purity_percentage

``Attribute Value`` autonames ``field:attribute_value``, so the row for the purity string
``"99.9"`` is named ``99.9`` and is shared by every item, every company and every test on
the site. On this bench that row carries ``purity_percentage = 100.0`` -- a live-master
defect recorded in the P00 manifest. A test that leans on it is therefore asserting
against a known-wrong value, and a test that "fixes" it would rewrite a live master used
by unrelated production data.

So every fixture here is **isolated**: purity values are declared explicitly in this
module, under ``CG-TEST-`` names that exist nowhere in the live masters. Nothing here
reads, creates or mutates ``99.9``, ``100.0`` or any other real Attribute Value.

FINE GRAMS vs REFERENCE GRAMS
-----------------------------
These are two different numbers and the codebase computes only one of them. Keeping them
apart is the entire point of this module::

	fine grams      = gross x item_purity / 100
	reference grams = gross x item_purity / reference_purity

``reference_purity`` is the purity of the Manufacturing Setting's ``pure_gold_item``.
``doc_events.stock_entry`` computes ``custom_pure_qty = item_purity * qty / pure_item_purity``,
which is **reference grams**. Fine grams are computed nowhere in the app.

The two coincide if and only if ``reference_purity == 100``. Because the usual reference
item is 24KT at 99.9, they normally differ -- and 10 reference grams and 9.990 fine grams
can both be correct descriptions of the same physical metal. Neither is "the" pure weight;
they answer different questions, so a test must say which one it means.

USAGE
-----
``purity_map()`` returns the explicit item -> purity mapping for pure-logic tests, which
patch ``get_purity_percentage`` with it and touch no database.

``ensure_attribute_values()`` creates the ``Attribute Value`` rows as real records for
integration evidence on a disposable site. It refuses to run unless the site sets
``customer_gold_disposable_site``, and it never touches a name outside the ``CG-TEST-``
namespace.

Those rows are only the LAST hop of the purity join. The ``Item Attribute``, the metal
template, the variants and their ``Item Variant Attribute`` links are built by
``test_customer_gold_integration``, because they need a company and an item group that
belong to that suite rather than to this module.
"""

import frappe
from frappe.utils import flt

#: Marks every record this module is allowed to create or modify.
FIXTURE_PREFIX = "CG-TEST-"

#: The Attribute used by the purity join. Real, shared, and never modified here.
METAL_PURITY_ATTRIBUTE = "Metal Purity"

#: Isolated purity values, declared explicitly rather than read from a master.
#:
#: Each key is BOTH the Attribute Value name and the Item code suffix, so a fixture's
#: purity is legible from its name and cannot silently drift from the value asserted in a
#: test.
PURITY_VALUES = {
	# -- reference candidates -------------------------------------------------------
	#: Notional true-pure metal. The ONLY purity at which reference grams == fine grams.
	"CG-TEST-100.0": 100.0,
	#: The real-world 24KT fine reference, and the usual ``pure_gold_item`` purity.
	"CG-TEST-99.9": 99.9,
	# -- operating purities ---------------------------------------------------------
	#: The real 75.4% operating case. NOT the 75.0 nominal for 18KT -- see below.
	"CG-TEST-75.4": 75.4,
	#: Nominal 18KT, carried purely so a test can show 75.0 and 75.4 are distinguishable
	#: and that neither may be substituted for the other.
	"CG-TEST-75.0": 75.0,
	#: 22KT.
	"CG-TEST-91.6": 91.6,
}

#: Item code -> purity. One item per purity, so an item's purity is unambiguous.
PURITY_ITEMS = {
	f"{FIXTURE_PREFIX}GOLD-{value}": value for value in PURITY_VALUES.values()
}

#: The reference item used by the Manufacturing Setting in integration fixtures.
#: 99.9, deliberately NOT 100.0 -- picking 100.0 would make reference and fine grams
#: coincide and the tests would stop being able to tell them apart.
DEFAULT_REFERENCE_ITEM = f"{FIXTURE_PREFIX}GOLD-99.9"
DEFAULT_REFERENCE_PURITY = 99.9

#: The real operating case named in the specification.
OPERATING_PURITY = 75.4
OPERATING_ITEM = f"{FIXTURE_PREFIX}GOLD-75.4"


def purity_map():
	"""Explicit item -> purity mapping for pure-logic tests. Reads no database."""
	return dict(PURITY_ITEMS)


def fine_grams(gross_qty, item_purity):
	"""Grams of pure metal contained, on an absolute 100% scale.

	Provided so a test can state the fine weight explicitly instead of implying it from
	``custom_pure_qty``.

	Deliberately unrounded -- callers compare against exact expected values. Note the app
	DOES compute on this basis elsewhere: ``sub_utils/snc.py`` and ``sub_utils/cg_settle.py``
	write ``qty * purity / 100`` into ``custom_pure_qty``, the same field
	``doc_events/stock_entry.py`` fills with REFERENCE grams. That collision is a separate
	open finding; this module does not resolve it.
	"""
	return flt(gross_qty) * flt(item_purity) / 100.0


def reference_grams(gross_qty, item_purity, reference_purity):
	"""Grams expressed in units of the reference item's purity.

	Mirrors the SCALED branch of ``doc_events.stock_entry``'s ``custom_pure_qty``
	(``flt((item_purity * qty) / pure_item_purity, 3)``), rounding included.

	NOT identical on the equal-purity branch: when ``pure_item_purity == item_purity`` the
	app short-circuits to ``row.custom_pure_qty = row.qty``, unrounded, rather than
	computing the quotient. The two agree numerically there for any sane input, but the
	code paths differ -- so a test asserting the equal-purity case should assert
	``row.qty`` directly rather than call this helper.
	"""
	if not reference_purity:
		raise ValueError("reference_purity is required and must be non-zero")
	return flt((flt(item_purity) * flt(gross_qty)) / flt(reference_purity), 3)


# ---------------------------------------------------------------------------------
# Real-record creation, for integration evidence on a disposable site only.
# ---------------------------------------------------------------------------------


#: The same explicit opt-in the integration suite requires.
DISPOSABLE_FLAG = "customer_gold_disposable_site"


def _assert_disposable_site():
	"""Refuse to create records unless the site explicitly opts in.

	Gated on ``customer_gold_disposable_site``, NOT on ``allow_tests``/``in_test``. The
	earlier version checked those and was inert: under ``bench run-tests``
	``frappe.flags.in_test`` is always True, so the throw could never fire from a test --
	precisely the situation it was meant to guard. ``allow_tests`` is no better; several
	real sites on this bench carry it.

	This flag is an operator opt-in, not proof the site is empty. Nothing here can verify
	that.
	"""
	if not frappe.conf.get(DISPOSABLE_FLAG):
		frappe.throw(
			f"customer_gold_purity_fixtures refuses to run on this site. It creates "
			f"Attribute Values and must never touch a site holding real data. Set "
			f"{DISPOSABLE_FLAG!r} in site_config.json only on a disposable site."
		)


def ensure_attribute_values():
	"""Create the isolated Attribute Values. Never touches a non-``CG-TEST-`` name."""
	_assert_disposable_site()

	created = []
	for name, purity in PURITY_VALUES.items():
		assert name.startswith(FIXTURE_PREFIX), name

		if frappe.db.exists("Attribute Value", name):
			# Re-assert the purity: a half-seeded site must converge on the declared
			# value rather than keep whatever a previous run left behind.
			if (
				flt(frappe.db.get_value("Attribute Value", name, "purity_percentage"))
				!= purity
			):
				frappe.db.set_value(
					"Attribute Value", name, "purity_percentage", purity
				)
			continue

		doc = frappe.get_doc(
			{
				"doctype": "Attribute Value",
				"attribute_value": name,
				"purity_percentage": purity,
			}
		)
		doc.insert(ignore_permissions=True)
		created.append(name)

	return created


def describe():
	"""Return the fixture table, for pasting into evidence documents."""
	rows = []
	for item, purity in sorted(PURITY_ITEMS.items()):
		rows.append(
			{
				"item": item,
				"item_purity": purity,
				"reference_purity": DEFAULT_REFERENCE_PURITY,
				"fine_grams_per_100g": fine_grams(100, purity),
				"reference_grams_per_100g": reference_grams(
					100, purity, DEFAULT_REFERENCE_PURITY
				),
			}
		)
	return rows
