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
``SUM(cint(pcs))`` over the request's item rows -- deliberately the same population and the
same cast as ``update_pure_qty``, so a backfilled value and a re-saved value agree. ``pcs``
is a Data column (varchar(140)), hence the CAST.

``NULLIF(pcs, '')`` because an empty string is not NULL: ``SUM`` skips NULL, and a request
with no pcs at all then aggregates to NULL, which ``COALESCE`` turns into 0 -- matching
``cint(None) == 0``.

``SIGNED`` and not ``UNSIGNED``: MariaDB wraps a negative string cast to UNSIGNED round to
18446744073709551615, which would write garbage. ``SIGNED`` matches ``cint`` on negatives.
No negative values exist on this bench (checked), but the field is free text and a future
row could carry one.

The one place SQL and ``cint`` disagree is a half-numeric string -- ``CAST('12abc')`` is 12
in MariaDB where ``cint('12abc')`` is 0. Zero rows of 28,976 are anything but plain digits,
so nothing is affected today; a row like that would simply be corrected on its next save.

WHY RAW SQL AND NOT ``frappe.db.set_value``
--------------------------------------------
Two reasons. ``set_value`` touches ``modified``, which would make every Material Request on
the site look edited -- and ``modified`` is what the fixture/sync machinery and several
reports key off. And it is per-document: 10,208 round trips against ~16k rows here, and
roughly 22x that on the production site.

Instead the delta is computed in one query and applied grouped BY VALUE -- 174 distinct
totals cover all 10,208 rows here -- with the names chunked into IN lists. Cancelled
requests are included: the field is informational, and leaving a cancelled request reading 0
beside a populated Total Quantity is the very inconsistency this patch removes.

Idempotent by construction: the driving query only returns rows whose stored value already
differs from the computed one, so a second run finds nothing and writes nothing. Ad-hoc
entry point::

    bench --site <site> execute jewellery_erpnext.patches.backfill_material_request_total_pcs.execute
"""

import frappe

DOCTYPE = "Material Request"
CHILD_DOCTYPE = "Material Request Item"
FIELDNAME = "custom_total_pcs"

# Names per UPDATE. Large enough that the statement count stays trivial, small enough to
# stay well inside max_allowed_packet at ~30 bytes a name.
CHUNK_SIZE = 5_000

# Requests whose stored total already differs from what the rows add up to. The
# parenttype/parentfield pair is pinned rather than assumed: filtering on ``parent`` alone
# would count a stray row from another table that happened to share a name.
PENDING_QUERY = """
	SELECT mr.name AS name, COALESCE(agg.total_pcs, 0) AS want
	FROM `tabMaterial Request` mr
	LEFT JOIN (
		SELECT parent, SUM(CAST(NULLIF(pcs, '') AS SIGNED)) AS total_pcs
		FROM `tabMaterial Request Item`
		WHERE parenttype = 'Material Request' AND parentfield = 'items'
		GROUP BY parent
	) agg ON agg.parent = mr.name
	WHERE mr.{fieldname} <> COALESCE(agg.total_pcs, 0)
"""


def _chunks(items, size):
	for start in range(0, len(items), size):
		yield items[start : start + size]


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

	pending = frappe.db.sql(PENDING_QUERY.format(fieldname=FIELDNAME), as_dict=True)
	if not pending:
		return 0

	# Grouped by value so the row count drives the work, not the statement count: every
	# request sharing a total is corrected by one UPDATE.
	by_value = {}
	for row in pending:
		by_value.setdefault(int(row.want), []).append(row.name)

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
