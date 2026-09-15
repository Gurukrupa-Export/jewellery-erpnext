"""DB-level backstop: a UNIQUE index on ``Serial No.custom_stamping_no``.

A stamping number ends up physically on a piece of jewellery, so two pieces must never
carry the same one. ``doc_events.serial_no.reserve_stamping_sequence`` is what actually
stops duplicates; this index is the guarantee that no FUTURE code path can slip one
through -- notably the ``frappe.db.bulk_insert`` route ERPNext uses to create Serial Nos
(``erpnext/stock/serial_batch_bundle.py``), which bypasses ``before_save`` entirely.

LOG-AND-SKIP ON DIRTY DATA
--------------------------
MariaDB refuses to build a unique index while duplicates exist. Duplicates created before
the collision fix are NOT renumbered here: the number is already on the metal, so choosing
which piece keeps it is a business decision, not a migration's. This patch reports them and
returns, leaving migrate green. Re-run it once they are resolved -- no code change needed.

WHY THE CUSTOM FIELD PROPERTY IS FLIPPED TOO
--------------------------------------------
A raw ``ALTER TABLE .. ADD UNIQUE`` alone is silently REVERTED. Frappe's schema sync reads
``column_key = 'UNI'`` as "currently unique" and, finding the docfield says otherwise, adds
the column to ``drop_unique`` (``frappe/database/schema.py:291-292``) and emits ``DROP INDEX``
(``frappe/database/mariadb/schema.py:131-139``) the next time anything re-syncs `tabSerial No`.

So the Custom Field must agree -- but it is set with ``frappe.db.set_value``, NOT a doc
save: saving the Custom Field triggers a full table sync that would hard-fail migrate on a
site that still has duplicates, and would also try to reconcile the column to the
``length: 10`` the field declares while the column is actually varchar(64).

The index NAME must be the fieldname: frappe's own add path emits
``ADD UNIQUE INDEX IF NOT EXISTS {fieldname}`` (``frappe/database/mariadb/schema.py:85``), so
matching it makes a future sync a no-op instead of a duplicate-index error. That is also why
``frappe.db.add_unique()`` is not used here -- it names the constraint ``unique_{fieldname}``,
which a later sync would not recognise as its own and would add a second index alongside.
"""

import frappe

TABLE = "tabSerial No"
FIELD = "custom_stamping_no"


def _index_exists(table: str, index_name: str) -> bool:
	return bool(
		frappe.db.sql(
			"""
			SELECT 1 FROM information_schema.statistics
			WHERE table_schema = DATABASE()
			  AND table_name = %s
			  AND index_name = %s
			LIMIT 1
			""",
			(table, index_name),
		)
	)


def _duplicates():
	return frappe.db.sql(
		f"""
		SELECT `{FIELD}`, COUNT(*) AS c, GROUP_CONCAT(`name`) AS serials
		FROM `{TABLE}`
		WHERE `{FIELD}` IS NOT NULL AND `{FIELD}` <> ''
		GROUP BY `{FIELD}`
		HAVING c > 1
		ORDER BY c DESC
		""",
		as_dict=True,
	)


def execute():
	if not frappe.db.has_column("Serial No", FIELD):
		return

	if _index_exists(TABLE, FIELD):
		_mark_custom_field_unique()
		return

	# Blank -> NULL. MariaDB permits many NULLs in a unique index but only one ''. These rows
	# never went through the stamping hook (bulk_insert, or a save from before the field
	# existed); nulling them loses nothing and they get stamped on their next save.
	frappe.db.sql(f"UPDATE `{TABLE}` SET `{FIELD}` = NULL WHERE `{FIELD}` = ''")

	duplicates = _duplicates()
	if duplicates:
		detail = "\n".join(f"  {d[FIELD]} x{d.c}: {d.serials}" for d in duplicates[:50])
		frappe.log_error(
			title="add_serial_no_stamping_unique_index: skipped, duplicates present",
			message=(
				f"{len(duplicates)} duplicated stamping number(s) in `{TABLE}`; the UNIQUE "
				f"index was NOT created and migrate was not failed.\n\n"
				f"New duplicates are already prevented by reserve_stamping_sequence. These "
				f"are historical rows -- each number is physically on a piece, so deciding "
				f"which piece keeps it is a business call. Re-run this patch once they are "
				f"resolved.\n\n{detail}"
			),
		)
		return

	# sql_ddl(), not sql(): the blank -> NULL UPDATE above leaves transaction_writes > 0, and
	# frappe refuses DDL in that state (ImplicitCommitError, database.py check_implicit_commit)
	# because ALTER TABLE autocommits in MariaDB and would silently commit those writes.
	# sql_ddl commits first, then runs the statement -- the same commit-then-alter frappe's own
	# db.add_index() / db.add_unique() do. Committing the normalisation here is correct: it is
	# idempotent, and the ALTER would have committed it regardless.
	frappe.db.sql_ddl(f"ALTER TABLE `{TABLE}` ADD UNIQUE INDEX `{FIELD}` (`{FIELD}`)")
	_mark_custom_field_unique()
	frappe.logger().info(
		f"add_serial_no_stamping_unique_index: UNIQUE index on {TABLE}.{FIELD} created"
	)


def _mark_custom_field_unique():
	"""Keep the docfield in step with the DB, or the next schema sync drops the index."""
	name = frappe.db.get_value(
		"Custom Field", {"dt": "Serial No", "fieldname": FIELD}, "name"
	)
	if not name:
		return
	if frappe.db.get_value("Custom Field", name, "unique"):
		return
	frappe.db.set_value("Custom Field", name, "unique", 1, update_modified=False)
	frappe.clear_cache(doctype="Serial No")
