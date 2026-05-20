"""Phase N — Composite indexes derived from Error Log forensics.

Complementary to ``add_jewellery_perf_indexes`` (which covers Employee IR /
Department IR / MOP Log voucher cascade). This patch adds the remaining
indexes identified by:

  /tmp/jewellery_existing_indexes.md     (Phase N-1 inventory)
  /tmp/jewellery_explain_before.md       (Phase N-4 EXPLAIN evidence)
  /tmp/jewellery_query_inventory.md      (Phase N-3 query extraction)
  context/error_log_row_map.csv          (Phase L 2,500-row classification)

Each ``_add_index_if_missing`` call documents its EG provenance + the
Phase B/D/E/H/I patch whose runtime idempotency lookup it accelerates.

**Idempotency**: re-runs are no-ops. ``_index_exists`` short-circuits when
the index already exists; ``_table_has_columns`` skips when a custom field
hasn't been migrated yet.

**Acceptance gate** (operator must complete BEFORE uncommenting a row):
  1. Capture EXPLAIN for the lookup query in /tmp/jewellery_explain_before.md.
  2. Confirm the chosen ``key`` is NULL or a worse index.
  3. Confirm scanned ``rows`` ≥ 50% reduction after `ALTER TABLE ADD INDEX`.
  4. Confirm Stock Entry submit latency increase ≤ 5% on staging.
  5. Re-run Phase I/J/K/M test suites — all green.

Provenance per row below uses the EG IDs from Phase L's row map.
"""

import frappe


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


def _table_has_columns(table: str, columns: tuple) -> bool:
	rows = frappe.db.sql(
		"""
		SELECT column_name FROM information_schema.columns
		WHERE table_schema = DATABASE() AND table_name = %s
		""",
		(table,),
	)
	present = {r[0] for r in rows}
	return all(c in present for c in columns)


def _add_index_if_missing(table: str, index_name: str, columns: tuple) -> None:
	if _index_exists(table, index_name):
		return
	if not _table_has_columns(table, columns):
		frappe.log_error(
			title=f"add_performance_indexes_from_error_log: skip {index_name}",
			message=(
				f"Table `{table}` is missing one or more of {columns}; "
				"index not added. Re-run after the doctype is fully migrated."
			),
		)
		return
	col_list = ", ".join(f"`{c}`" for c in columns)
	frappe.db.sql(f"ALTER TABLE `{table}` ADD INDEX `{index_name}` ({col_list})")


def execute():
	"""Each spec MUST have EXPLAIN evidence before uncommenting.

	Until the operator runs Phase N-4 on a copied site and captures
	/tmp/jewellery_explain_before.md, every row stays commented and the
	patch is a no-op.

	An empty execute() is intentionally idempotent — no migration risk
	from shipping an unpopulated patch.
	"""
	specs = (
		# ===== EG-002 — Product Certification idempotency lookup =====
		# Query: doc_events/material_request.py + product_certification.py
		#   SELECT name, docstatus FROM `tabStock Entry`
		#   WHERE product_certification = %s AND docstatus < 2 LIMIT 1
		# Phase B EG-002 + Phase H D-002 idempotency call this on every
		# Product Certification submit. Index narrows from a full-table
		# scan to a single row.
		# (
		# 	"tabStock Entry",
		# 	"se_product_cert_idx",
		# 	("product_certification", "docstatus"),
		# ),
		# ===== EG-003 — Serial Number Creator idempotency lookup =====
		# Query: doctype/serial_number_creator/serial_number_creator.py:169
		#   frappe.db.get_value("Stock Entry",
		#       {"custom_serial_number_creator": self.name, "docstatus": ["<", 2]})
		# Phase B EG-003 + Phase I D-003 draft-adoption flag.
		# (
		# 	"tabStock Entry",
		# 	"se_snc_idx",
		# 	("custom_serial_number_creator", "docstatus"),
		# ),
		# ===== EG-004 — Employee IR injection idempotency =====
		# Query: doctype/employee_ir/doc_events/main_slip_inject.py:741
		#   SELECT 1 FROM `tabStock Entry`
		#   WHERE employee_ir = %s AND custom_eir_operation_row = %s
		#     AND auto_created = 1 AND docstatus < 2 LIMIT 1
		# Phase B EG-004. Composite key on 4 columns; verify selectivity
		# of (employee_ir, custom_eir_operation_row) is high before adding.
		# (
		# 	"tabStock Entry",
		# 	"se_eir_op_idx",
		# 	("employee_ir", "custom_eir_operation_row", "auto_created", "docstatus"),
		# ),
		# ===== EG-001A/B — Material Request Reserve idempotency =====
		# D-001 caveat: material_request column lives on tabStock Entry
		# Detail (child), NOT tabStock Entry (parent). The Phase B JOIN
		# query at material_request.py:425 is:
		#   SELECT se.name, se.docstatus FROM `tabStock Entry` se
		#   JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		#   WHERE sed.material_request = %s
		#     AND se.purpose = 'Material Transfer'
		#     AND se.add_to_transit = 1 AND se.docstatus < 2
		# Index belongs on the child to accelerate the JOIN's filter
		# predicate. tabStock Entry Detail is write-heavy; verify
		# write-cost ≤ 5% on copied site before deploying.
		# (
		# 	"tabStock Entry Detail",
		# 	"sed_mr_idx",
		# 	("material_request",),
		# ),
		# ===== EG-011 — MOP Log balance lookup =====
		# Query: doctype/mop_log/mop_log.py::get_current_mop_balance_rows
		#   ORDER BY creation DESC LIMIT 1 per (manufacturing_operation,
		#   item_code, batch_no). The leading three columns are
		#   high-selectivity; adding creation lets the optimizer use the
		#   index for the ORDER BY too.
		# (
		# 	"tabMOP Log",
		# 	"mop_log_balance_idx",
		# 	("manufacturing_operation", "item_code", "batch_no", "docstatus", "creation"),
		# ),
		# ===== S-2 — Submission Queue parent-docstatus dedupe =====
		# Query: customization/submission_queue/submission_queue.py:9
		#   frappe.db.get_value("Submission Queue",
		#       {"ref_doctype": self.ref_doctype,
		#        "ref_docname": self.ref_docname,
		#        "status": ["in", ["Queued", "Finished"]]})
		# Without this index a high-traffic site scans the queue table on
		# every submit attempt. tabSubmission Queue grows over time.
		# (
		# 	"tabSubmission Queue",
		# 	"subq_ref_status_idx",
		# 	("ref_doctype", "ref_docname", "status"),
		# ),
	)
	for table, index_name, columns in specs:
		_add_index_if_missing(table, index_name, columns)
