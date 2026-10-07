"""Report which submitted Stock Entry rows the restored ownership guard would refuse.

``se_utils.validate_inventory_dimention`` enforces that a consuming row draws the owner's
material its Parent Manufacturing Order was placed on. It was commented out by 0040324c
("fix: sync v15 updates with v16", 8 May 2026) -- a 17-file bulk sync that gave no reason -- and
stayed dead, so this site holds submitted Stock Entries that nothing ever checked.

The guard therefore ships in warn-only mode (``WARN_ONLY_PMO_ROW_OWNERSHIP``). This patch is how
you decide when to turn it on: it applies the guard's own rules, through the guard's own helpers,
to history, and counts what would have been refused. It CHANGES NOTHING.

Read it before flipping the switch. A manufacturer with a large count is either doing something
the business actually allows -- in which case tick ``custom_allow_regular_goods_instead_of_customer
_goods`` on that Manufacturer, which downgrades the two "wrong lane" refusals to a warning -- or it
has been mislabelling a customer's material as the company's, which is the reason the guard exists.

Diamonds and gemstones are the exception: on a hand-built entry the guard throws whatever the
switch or the allowance says -- the customer's goods when the order ticks them, company stock
when it does not -- so those rows are always counted as errors.

Idempotent, and safe to run ad hoc as often as you like::

    bench --site <site> execute jewellery_erpnext.patches.audit_pmo_row_ownership.execute

A different window, in days::

    bench --site <site> execute jewellery_erpnext.patches.audit_pmo_row_ownership.execute --kwargs "{'days': 365}"
"""

import frappe
from frappe.utils import add_days, nowdate

from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
	CUSTOMER_INVENTORY_TYPES,
	normalize_ownership,
	pmo_expects_customer_goods,
	STRICT_CUSTOMER_GOODS_VARIANTS,
)

#: How far back to look. The guard died on 2026-05-08, so a year covers it with room to spare.
DEFAULT_DAYS = 365

#: Rows scanned at most, so an audit on a large site cannot become the slow query that wedges a
#: migrate. Reported when hit, rather than silently truncating the counts.
ROW_CAP = 200000

WRONG_OWNER = "consumes another customer's material"
WANTED_CUSTOMER_GOODS = "company stock on a customer-supplied order"
WANTED_COMPANY_STOCK = "customer goods on a company order"


def execute(days=DEFAULT_DAYS):
	rows = _rows(int(days))
	if not rows:
		print(
			f"PMO row ownership audit: no submitted consuming rows with an order in the last "
			f"{days} days. Nothing to weigh."
		)
		return

	pmo_map = _pmo_map(rows)
	findings = []

	for row in rows:
		# The batch is the physical truth, exactly as ``resolve_batch_ownership`` reads it at
		# validate time -- the row's own inventory_type/customer are its claim about itself,
		# and the claim is what goes wrong.
		inventory_type, customer = normalize_ownership(
			row.batch_inventory_type or row.inventory_type,
			row.batch_customer or row.customer,
			batch_no=row.batch_no,
			item_code=row.item_code,
		)
		for pmo_name in str(row.pmo or "").split(","):
			pmo = pmo_map.get(pmo_name.strip())
			if not pmo:
				continue

			reason = classify_row(inventory_type, customer, pmo, row.variant_of)
			if reason:
				# The guard blocks a diamond or gemstone row drawing the wrong owner outright, on any
				# entry built by hand -- the allowance does not reach it.
				strict = not row.auto_created and row.variant_of in STRICT_CUSTOMER_GOODS_VARIANTS
				findings.append((pmo.manufacturer, bool(pmo.allow_substitution), reason, strict))

	_print(rows, findings, days)


def classify_row(inventory_type, customer, pmo, variant_of):
	"""The guard's verdict for one row/order pair, or None when the row would pass.

	The three rules, in ``validate_inventory_dimention``'s order and through the same shared
	helpers, so the audit cannot quietly disagree with the thing it is measuring. Separated from
	:func:`execute` so it can be tested -- an audit people will trust to decide whether to turn
	enforcement on is not something to leave unexercised.
	"""
	is_customer_owned = inventory_type in CUSTOMER_INVENTORY_TYPES
	expects_customer_goods = pmo_expects_customer_goods(pmo, variant_of)

	if is_customer_owned and customer != (pmo or {}).get("customer"):
		return WRONG_OWNER

	if expects_customer_goods and not is_customer_owned:
		return WANTED_CUSTOMER_GOODS

	if is_customer_owned and not expects_customer_goods:
		return WANTED_COMPANY_STOCK


def _is_error(allowed, reason, strict):
	"""Would the guard THROW for this finding, rather than merely warn?

	WRONG_OWNER ignores the allowance: permission to use company stock in the customer's place says
	nothing about consuming a THIRD party's goods. A strict finding -- a diamond or gemstone on a
	hand-built entry -- ignores it as well.
	"""
	return reason == WRONG_OWNER or strict or not allowed

	return None


def _rows(days):
	"""Submitted CONSUMING rows carrying an order, with their batch's ownership and item variant.

	Scoped exactly as the guard is: ``s_warehouse`` set (a pure inward row drew nothing, and a
	Customer Gold receipt has no batch until ``create_parent_batches`` mints one at submit), and
	the row's own order else the entry's -- a hand-built transfer names it only in the header.
	"""
	return frappe.db.sql(
		"""
		SELECT
			COALESCE(
				NULLIF(sed.custom_parent_manufacturing_order, ''), se.manufacturing_order
			)                                      AS pmo,
			se.auto_created                        AS auto_created,
			sed.item_code                          AS item_code,
			sed.batch_no                           AS batch_no,
			sed.inventory_type                     AS inventory_type,
			sed.customer                           AS customer,
			b.custom_inventory_type                AS batch_inventory_type,
			b.custom_customer                      AS batch_customer,
			i.variant_of                           AS variant_of
		FROM `tabStock Entry Detail` sed
		INNER JOIN `tabStock Entry` se ON se.name = sed.parent
		LEFT JOIN `tabBatch` b ON b.name = sed.batch_no
		LEFT JOIN `tabItem` i ON i.name = sed.item_code
		WHERE se.docstatus = 1
		  AND se.posting_date >= %(since)s
		  AND IFNULL(sed.s_warehouse, '') != ''
		  AND (
			IFNULL(sed.custom_parent_manufacturing_order, '') != ''
			OR IFNULL(se.manufacturing_order, '') != ''
		  )
		LIMIT %(cap)s
		""",
		{"since": add_days(nowdate(), -days), "cap": ROW_CAP},
		as_dict=True,
	)


def _pmo_map(rows):
	"""``{pmo: flags + customer + the manufacturer's substitution allowance}`` in two queries."""
	names = {
		part.strip()
		for row in rows
		for part in str(row.pmo or "").split(",")
		if part.strip()
	}
	if not names:
		return {}

	orders = {
		o.name: o
		for o in frappe.get_all(
			"Parent Manufacturing Order",
			filters={"name": ["in", list(names)]},
			fields=[
				"name",
				"customer",
				"manufacturer",
				"is_customer_gold",
				"is_customer_diamond",
				"is_customer_gemstone",
				"is_customer_material",
			],
		)
	}

	manufacturers = {o.manufacturer for o in orders.values() if o.manufacturer}
	allowances = (
		{
			m.name: m.custom_allow_regular_goods_instead_of_customer_goods
			for m in frappe.get_all(
				"Manufacturer",
				filters={"name": ["in", list(manufacturers)]},
				fields=["name", "custom_allow_regular_goods_instead_of_customer_goods"],
			)
		}
		if manufacturers
		else {}
	)
	for order in orders.values():
		order.allow_substitution = allowances.get(order.manufacturer)

	return orders


def _print(rows, findings, days):
	print("")
	print(f"PMO row ownership audit -- last {days} days, {len(rows)} consuming rows scanned")
	if len(rows) >= ROW_CAP:
		print(f"  NOTE: the {ROW_CAP} row cap was hit; counts below are a lower bound.")

	if not findings:
		print("  Nothing would be refused. Safe to set WARN_ONLY_PMO_ROW_OWNERSHIP = False.")
		print("")
		return

	# A refusal the manufacturer's own allowance already downgrades to a warning is not a
	# blocker, so the two are counted apart -- only the hard ones decide the flip.
	hard = [f for f in findings if _is_error(f[1], f[2], f[3])]
	print(f"  {len(findings)} row/order pairs would be reported, {len(hard)} of them as errors.")
	print("")
	print(f"  {'Manufacturer':<28} {'Reason':<44} {'Errors':>7} {'Warnings':>9}")
	print(f"  {'-' * 28} {'-' * 44} {'-' * 7} {'-' * 9}")

	tally = {}
	for manufacturer, allowed, reason, strict in findings:
		key = (manufacturer or "(none)", reason)
		counts = tally.setdefault(key, [0, 0])
		counts[0 if _is_error(allowed, reason, strict) else 1] += 1

	for (manufacturer, reason), (errors, warnings) in sorted(
		tally.items(), key=lambda kv: -kv[1][0]
	):
		print(f"  {manufacturer:<28} {reason:<44} {errors:>7} {warnings:>9}")

	print("")
	print("  Next: either tick 'Allow Regular Goods Instead Of Customer Goods' on the")
	print("  manufacturers whose counts are legitimate, or fix the data -- then set")
	print("  WARN_ONLY_PMO_ROW_OWNERSHIP = False in customization/stock_entry/doc_events/se_utils.py.")
	print("")
