"""Neutralize duplicate Moulds per Item so a UNIQUE index can be added to
``Mould.item_code``.

The app enforces one Mould per Item in ``Mould.validate`` (``validate_unique_item_code``),
but historically the Employee IR casting flow auto-created a Mould per casting op, so
existing sites can hold several Moulds for the same Item. ``mould.json`` now marks
``item_code`` unique, which makes model-sync run ``ADD UNIQUE INDEX`` on ``tabMould`` --
that aborts with a 1062 duplicate-entry error unless duplicates are cleared first.

Strategy (non-destructive): for each Item with >1 non-cancelled Mould, keep the earliest
(``ORDER BY creation ASC, name ASC``) and set the losers' ``item_code`` to NULL via a
direct DB write (no hooks -- avoids ``on_trash`` nulling the kept ``Item.mould`` cache).
The losing Mould records are preserved (physical rake/tray/box data kept); MariaDB allows
repeated NULLs under a UNIQUE index; and any already-propagated ``mould_id`` (a plain Data
string holding the Mould docname) stays valid because the record still exists under the
same name.

Runs in ``[pre_model_sync]`` so it completes BEFORE model-sync adds the unique index.
No-op on a site with <=1 Mould per Item (fresh CI) and idempotent on re-run.

Ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.dedupe_mould_item_code.execute
"""

import frappe


def execute():
	if not frappe.db.table_exists("Mould"):
		return

	dupe_item_codes = frappe.db.sql(
		"""
		SELECT item_code
		FROM `tabMould`
		WHERE docstatus < 2 AND item_code IS NOT NULL AND item_code != ''
		GROUP BY item_code
		HAVING COUNT(*) > 1
		""",
		as_dict=True,
	)

	neutralized = []
	for row in dupe_item_codes:
		moulds = frappe.db.sql(
			"""
			SELECT name
			FROM `tabMould`
			WHERE item_code = %s AND docstatus < 2
			ORDER BY creation ASC, name ASC
			""",
			row.item_code,
			as_dict=True,
		)
		# Keep the earliest; neutralize the rest so item_code becomes unique.
		for loser in moulds[1:]:
			frappe.db.set_value(
				"Mould", loser.name, "item_code", None, update_modified=False
			)
			neutralized.append(loser.name)

	if neutralized:
		frappe.logger().info(
			"dedupe_mould_item_code: cleared item_code on {} duplicate Mould(s): {}".format(
				len(neutralized), ", ".join(neutralized)
			)
		)
