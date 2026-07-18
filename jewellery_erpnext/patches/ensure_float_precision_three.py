"""
Raise System Settings ``float_precision`` from 2 to 3 so the stock ledger can hold the third
decimal that jewellery weights are actually recorded in.

WHY THIS EXISTS
---------------
Metal is booked in grams and diamonds in carats to 3 decimals -- a repair unpack books e.g.
0.122 ct off the design BOM, and 52% of this site's Stock Ledger Entries carry a non-zero 3rd
decimal. The app already pins the precision-3 fields it can:
``Stock Entry Detail.transfer_qty``, ``Serial and Batch Entry.qty`` and the Stock Reservation
Entry qty fields (see ``property_setter_guard``). But the ledger BALANCE is not a field-precision
decision -- erpnext/stock/stock_ledger.py reads the GLOBAL default::

    self.flt_precision = cint(frappe.db.get_default("float_precision")) or 2      # :525
    sle.qty_after_transaction = flt(self.wh_data.qty_after_transaction, self.flt_precision)  # :966

So with ``float_precision = 2`` no Property Setter can stop a 0.122 booking from settling as a
0.12 ledger balance while its Serial and Batch Bundle still says 0.122. That divergence is not
cosmetic:

* ``get_batch_qty(batch) = 0.072`` vs ``Bin.actual_qty = 0.07`` -- consuming the batch in full
  drives the ledger negative (BatchNegativeStockError).
* ERPNext's ``validate_with_allowed_qty`` re-checks reservations against the WAREHOUSE ledger, so
  reserving a precision-3 batch qty throws ``Cannot reserve more than Allowed Qty`` whenever the
  3rd decimal rounds DOWN (0.122 -> 0.12 throws; 0.098 -> 0.1 happens to pass).

``float_precision`` is a Select with options 2..9, and System Settings' ``on_update`` ->
``set_defaults()`` calls ``frappe.db.set_default(fieldname, value)`` for each such field
(frappe/core/doctype/system_settings/system_settings.py), which is exactly what ``stock_ledger``
reads -- so saving the Single is the supported way to propagate this.

HOW THE SETTING GOT LOST (and how it will get lost again)
---------------------------------------------------------
3 was the ORIGINAL setting on the production site. It was silently reverted to 2, and the
System Settings version history names the culprit -- a single edit flipping BOTH
``float_precision`` and ``currency_precision`` from "3" to "2". That is the exact signature of
``erpnext/tests/utils.py::BootStrapTestData.update_system_settings``::

    system_settings.currency_precision = system_settings.float_precision = 2

``BootStrapTestData()`` is instantiated at MODULE level, so merely *importing*
``erpnext.tests.utils`` -- which any erpnext-dependent test does, transitively -- rewrites
System Settings on whatever site is connected. It assumes a throwaway test site. Run such a
suite against a real site and it rewrites time_zone, language, rounding_method,
currency_precision and float_precision, and the sub-0.01 rounding bugs come straight back.

So: never point erpnext-dependent tests at a live site. If this setting is ever found back at 2,
that is almost certainly why -- and note ``bench migrate`` will NOT restore it, because this
patch is already in the Patch Log and will not re-run. Re-apply with::

    bench --site <site> execute jewellery_erpnext.patches.ensure_float_precision_three.ensure_float_precision

SCOPE / LIMITS (deliberate):
* Only ever RAISES. If a site already runs 3+ this is a no-op; we never lower a site's precision.
* Fixes new postings only. Existing SLE/Bin balances keep their 2-dp values -- the drift is frozen,
  not healed. Healing historical balances needs a Repost Item Valuation and is not attempted here.
* Does NOT touch ``currency_precision``, which the same test-bootstrap knocked 3 -> 2. That is a
  money-rounding decision, left for a human.

Wired in two places (both idempotent): a ``post_model_sync`` patch entry for existing sites, and
``create_test_data.setup_data`` for fresh / CI sites -- the same two-place wiring
``property_setter_guard`` uses, because the ``after_migrate`` hook is disabled in this app.
"""

import frappe
from frappe.utils import cint

MIN_FLOAT_PRECISION = 3


def ensure_float_precision():
	"""Raise System Settings float_precision to 3 if it is lower. Idempotent; never lowers.

	Checks BOTH places the value lives, because they can disagree and only one of them is
	the one that counts:

	* ``tabSingles`` -- what the System Settings form shows.
	* ``tabDefaultValue`` -- what ``frappe.db.get_default("float_precision")`` returns, and
	  therefore what erpnext's stock ledger actually rounds balances with.

	Only ``System Settings.save()`` propagates Singles -> DefaultValue (via ``on_update`` ->
	``set_defaults``). A plain ``frappe.db.set_single_value("System Settings",
	"float_precision", ...)`` writes Singles ONLY -- ``create_test_data.setup_data`` does
	exactly that -- so keying the guard off the Single alone would early-return on a site
	whose ledger is still silently rounding to 2.
	"""
	single_value = cint(
		frappe.db.get_single_value("System Settings", "float_precision")
	)
	propagated = cint(frappe.db.get_default("float_precision"))
	if single_value >= MIN_FLOAT_PRECISION and propagated >= MIN_FLOAT_PRECISION:
		return False

	settings = frappe.get_single("System Settings")
	if cint(settings.float_precision) < MIN_FLOAT_PRECISION:
		settings.float_precision = str(MIN_FLOAT_PRECISION)
	settings.flags.ignore_permissions = True
	# A fresh CI site (``bench new-site`` with no setup wizard) leaves System Settings'
	# mandatory ``language`` and ``time_zone`` NULL, so a plain save() dies with
	# MandatoryError before it can propagate anything. We only ever touch float_precision
	# here, so skip the mandatory check -- validate() and on_update->set_defaults() still
	# run, so the propagation below is unaffected. On real sites both fields are already
	# populated, making this a no-op there.
	settings.flags.ignore_mandatory = True
	# save() -> on_update -> set_defaults() -> frappe.db.set_default("float_precision", "3").
	settings.save()

	# Belt and braces: if the Single was already 3, save() may short-circuit as a no-op and
	# never propagate, leaving the ledger on the stale default. Assert it directly.
	if cint(frappe.db.get_default("float_precision")) < MIN_FLOAT_PRECISION:
		frappe.db.set_default("float_precision", str(MIN_FLOAT_PRECISION))
	return True


def execute():
	ensure_float_precision()
