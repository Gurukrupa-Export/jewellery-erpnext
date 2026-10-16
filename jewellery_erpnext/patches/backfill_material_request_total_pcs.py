"""Backfill ``Material Request.custom_total_pcs`` on requests that predate the field.

``add_material_request_total_pcs_field`` creates the field, and ``update_pure_qty`` fills it
on every save from then on -- but a save is the ONLY thing that fills it. Submitted and
cancelled requests can never run ``before_validate`` again, so without this they would read
0 forever, and drafts would read 0 until someone happened to re-open them. Measured on this
bench: 4,606 submitted + 399 cancelled are permanently stuck, and 11,249 drafts are stale
until touched. Total Quantity sits right beside Total Pcs on the form and has been computed
at save time since day one, so the asymmetry reads as a bug to anyone comparing the two.

WHAT IT COMPUTES
----------------
``sum(cint(row.pcs))`` over the request's item rows, in PYTHON, calling the very same
``frappe.utils.cint`` that ``update_pure_qty`` calls. That is the whole point: a backfilled
value and the value the next save computes are then equal by construction rather than by
argument, so this patch can never drift from the runtime calculation.

An earlier revision aggregated in SQL with ``SUM(CAST(NULLIF(pcs, '') AS SIGNED))``, which is
far cheaper but is NOT the same function. ``pcs`` is a Data column (varchar(140)) holding free
text, and the two engines disagree on real inputs -- measured, both ways:

===========  ==========================  ==========
value        ``CAST(... AS SIGNED)``     ``cint``
===========  ==========================  ==========
``'12abc'``  12                          **0**
``'1e3'``    1                           **1000**
``'1.9'``    1                           1
``' 5 '``    5                           5
``'-3'``     -3                          -3
``'abc'``    0                           0
``''``       NULL -> 0                   0
NULL         NULL -> 0                   0
===========  ==========================  ==========

Only the first two rows diverge, and no such value exists on this bench today (0 of 29,087).
But this patch runs against production, which is far larger, and the failure is silent: the
backfilled total would simply disagree with the total the document reports after its next
save. Paying a full table scan to remove that class of bug is the right trade.

Cancelled requests are included: the field is informational, and leaving a cancelled request
reading 0 beside a populated Total Quantity is the very inconsistency this patch removes.

WHY RAW SQL AND NOT ``frappe.db.set_value``
--------------------------------------------
Two reasons. ``set_value`` touches ``modified``, which would make every Material Request on
the site look edited -- and ``modified`` is what the fixture/sync machinery and several
reports key off. And it is per-document: 10,208 round trips against ~16k rows here, and
roughly 22x that on the production site.

Instead the delta is computed first and applied grouped BY VALUE -- 174 distinct totals cover
all 10,208 rows here -- with the names chunked into IN lists.

Idempotent by construction: only requests whose stored value already differs from the computed
one are written, so a second run finds nothing and writes nothing. Ad-hoc entry point::

    bench --site <site> execute jewellery_erpnext.patches.backfill_material_request_total_pcs.execute
"""

import frappe
from frappe.utils import cint

DOCTYPE = "Material Request"
CHILD_DOCTYPE = "Material Request Item"
FIELDNAME = "custom_total_pcs"

# Names per UPDATE. Large enough that the statement count stays trivial, small enough to
# stay well inside max_allowed_packet at ~30 bytes a name.
CHUNK_SIZE = 5_000

# Rows per SELECT while scanning. The scan is keyset-paged on ``name`` rather than OFFSET so
# the cost stays flat: ~29k child rows here, ~640k on production.
SCAN_SIZE = 20_000

# The parenttype/parentfield pair is pinned rather than assumed: filtering on ``parent``
# alone would count a stray row from another child table that happened to share a name.
ITEM_SCAN_QUERY = """
	SELECT name, parent, pcs
	FROM `tabMaterial Request Item`
	WHERE parenttype = 'Material Request' AND parentfield = 'items' AND name > %(after)s
	ORDER BY name
	LIMIT %(limit)s
"""

PARENT_SCAN_QUERY = """
	SELECT name, `{fieldname}` AS stored
	FROM `tabMaterial Request`
	WHERE name > %(after)s
	ORDER BY name
	LIMIT %(limit)s
"""


def _chunks(items, size):
	for start in range(0, len(items), size):
		yield items[start : start + size]


def _scan(query, **params):
	"""Yield every row of ``query``, keyset-paged on ``name``.

	Paged rather than fetched whole: this runs over every Material Request Item on the site,
	and OFFSET paging would re-walk the index on each page.

	The cursor must strictly advance or the loop stops. Against the database it always does
	-- ``name`` is the primary key and the query is ``ORDER BY name`` -- but a caller that
	stubs ``frappe.db.sql`` with a fixed result set would otherwise spin forever, which is a
	hang rather than a failed assertion and costs far more to diagnose than this guard.
	"""
	after = ""
	while True:
		rows = frappe.db.sql(
			query, {"after": after, "limit": SCAN_SIZE, **params}, as_dict=True
		)
		if not rows:
			return

		yield from rows

		last = rows[-1].name
		if last <= after:
			return
		after = last


def computed_totals():
	"""``{material_request: sum(cint(pcs))}`` for every request that has item rows.

	``cint`` and not SQL ``CAST``: see the module docstring for the inputs on which the two
	disagree. This is the function ``update_pure_qty`` uses, which is what makes the
	backfilled value and the next-save value the same number.
	"""
	totals = {}
	for row in _scan(ITEM_SCAN_QUERY):
		totals[row.parent] = totals.get(row.parent, 0) + cint(row.pcs)
	return totals


def pending_updates(totals):
	"""``{wanted_total: [material_request, ...]}`` for requests whose stored value is wrong.

	Grouped by value on the way out so the write path issues one UPDATE per distinct total
	rather than one per document. A request with no item rows wants 0.
	"""
	by_value = {}
	for row in _scan(PARENT_SCAN_QUERY.format(fieldname=FIELDNAME)):
		want = totals.get(row.name, 0)
		if cint(row.stored) != want:
			by_value.setdefault(want, []).append(row.name)
	return by_value


def backfill():
	"""Apply the backfill. Returns the number of Material Requests updated."""
	if not frappe.db.has_column(DOCTYPE, FIELDNAME):
		# The field patch has not run yet -- nothing to fill, and the query below would
		# fail on an unknown column. Ordering in patches.txt makes this unreachable on a
		# normal migrate; it matters when this is invoked ad-hoc.
		frappe.logger().warning(
			f"backfill_material_request_total_pcs: {DOCTYPE}.{FIELDNAME} missing, skipped"
		)
		return 0

	by_value = pending_updates(computed_totals())
	if not by_value:
		return 0

	updated = 0
	for value, names in by_value.items():
		for chunk in _chunks(names, CHUNK_SIZE):
			frappe.db.sql(
				f"""
				UPDATE `tab{DOCTYPE}`
				SET `{FIELDNAME}` = %(value)s
				WHERE name IN %(names)s
				""",
				{"value": value, "names": tuple(chunk)},
			)
			updated += len(chunk)

		# Committed per value rather than once at the end: on the production site this
		# spans far more rows, and a single transaction over all of them would hold row
		# locks on most of the table for the duration.
		frappe.db.commit()

	return updated


def execute():
	updated = backfill()
	frappe.logger().info(
		f"backfill_material_request_total_pcs: updated {updated} Material Request(s)"
	)
