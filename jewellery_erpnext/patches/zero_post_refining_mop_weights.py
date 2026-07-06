import frappe


def execute():
	"""Zero phantom weights on Manufacturing Operations created after refining.

	A submitted Work Order Refining Entry zeroes the MWO and its then-existing
	operations, but operations created AFTERWARDS (Department IR issue to the next
	department) re-imported the pre-refining weights two ways:

	  1. ``frappe.copy_doc`` in ``create_operation_for_next_dept`` copies no_copy
	     weight buckets (default ``ignore_no_copy=True``);
	  2. the Department IR / Employee IR MOP Log clones carried the source
	     operation's stale pre-refining ``qty_after_transaction`` rows, whose
	     validate recomputed the buckets back to the pre-refining figures.

	Both vectors are now closed in code (guarded by ``is_mwo_refined``); this patch
	repairs the documents already written on live sites:

	  - cancels Department IR / Employee IR MOP Log clones on operations created
	    after the Refining Entry (they would not exist under the fixed code), then
	    recomputes those operations from the remaining active rows;
	  - zeroes the weight buckets on those post-refining operations so the doc
	    matches the now-empty ledger.

	Scope is deliberately limited to operations created AFTER the refining Stock
	Entry — the exact "gross_wt reappears on a new operation after refining" bug.
	Pre-refining operations are left untouched: complete_refining already zeroed
	the work order's then-existing operations at the right time, and any weight one
	carries now is its own real MOP Log history, not a post-refining phantom.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		recalculate_manufacturing_operation_weights,
	)

	refined = frappe.db.sql(
		"""
		SELECT DISTINCT d.manufacturing_work_order AS mwo, re.name AS re_name,
		       re.creation AS re_creation
		FROM `tabManufacturing Work Order Refining Details` d
		INNER JOIN `tabRefining Entry` re ON re.name = d.parent
		WHERE d.parenttype = 'Refining Entry'
		  AND re.docstatus = 1
		  AND re.refining_type = 'Work Order Refining'
		  AND IFNULL(d.manufacturing_work_order, '') != ''
		""",
		as_dict=True,
	)
	if not refined:
		print("zero_post_refining_mop_weights: no refined MWOs found")
		return

	# The metal leaves at SUBMIT (when the transfer Stock Entry is written), not at
	# draft creation — an operation created while the Refining Entry sat in Draft is
	# a legitimate pre-refining operation and must not be treated as phantom. Use
	# the earliest Stock Entry the Refining Entry created as the cutoff; fall back
	# to the entry's creation only when no linked SE exists.
	cutoff_by_re = {
		row.parent: row.first_se
		for row in frappe.db.sql(
			"""
			SELECT custom_refining_entry AS parent, MIN(creation) AS first_se
			FROM `tabStock Entry`
			WHERE custom_refining_entry IN %s AND docstatus = 1
			GROUP BY custom_refining_entry
			""",
			(tuple({r.re_name for r in refined}),),
			as_dict=True,
		)
	}

	# An MWO can appear in several refining entries; keep the earliest cutoff so
	# every operation created after ANY refining of it is treated as phantom.
	re_creation_by_mwo = {}
	for row in refined:
		cutoff = cutoff_by_re.get(row.re_name) or row.re_creation
		cur = re_creation_by_mwo.get(row.mwo)
		if not cur or cutoff < cur:
			re_creation_by_mwo[row.mwo] = cutoff

	zero_map = {
		"qty": 0,
		"gross_wt": 0,
		"net_wt": 0,
		"finding_wt": 0,
		"diamond_wt": 0,
		"diamond_wt_in_gram": 0,
		"diamond_pcs": 0,
		"gemstone_wt": 0,
		"gemstone_wt_in_gram": 0,
		"gemstone_pcs": 0,
		"other_wt": 0,
		"prev_gross_wt": 0,
		"received_gross_wt": 0,
		"received_net_wt": 0,
		"loss_wt": 0,
	}

	cancelled_logs = zeroed_mops = 0
	synced_phantoms = []
	for mwo, re_creation in re_creation_by_mwo.items():
		post_refining_mops = frappe.db.get_all(
			"Manufacturing Operation",
			filters={
				"manufacturing_work_order": mwo,
				"creation": [">", re_creation],
			},
			pluck="name",
		)

		# 1. Drop the phantom ledger clones so nothing recomputes the weights back.
		if post_refining_mops:
			phantom_logs = frappe.db.get_all(
				"MOP Log",
				filters={
					"manufacturing_operation": ["in", post_refining_mops],
					"voucher_type": ["in", ["Department IR", "Employee IR"]],
					"is_cancelled": 0,
				},
				fields=["name", "is_synced", "manufacturing_operation"],
			)
			for log in phantom_logs:
				# A phantom row already pushed to the physical SLE by EOD sync
				# (is_synced=1) means phantom physical stock was created too.
				# Cancelling the MOP Log fixes the logical ledger but NOT the SLE;
				# flag those so a human can run SBB / batch-qty repair on the
				# affected warehouse (out of scope for a weight-zeroing patch).
				if log.is_synced:
					synced_phantoms.append(log.manufacturing_operation)
				frappe.db.set_value(
					"MOP Log", log.name, "is_cancelled", 1, update_modified=False
				)
			cancelled_logs += len(phantom_logs)
			if phantom_logs:
				for mop in post_refining_mops:
					recalculate_manufacturing_operation_weights(mop)

		# 2. Zero the doc-level buckets — ONLY on operations created AFTER the
		# refining Stock Entry. These never held real metal (their weight came
		# entirely from the Department IR / Employee IR clones cancelled above),
		# so after the recompute their buckets are 0 and this makes the doc match.
		#
		# Deliberately NOT touching pre-refining operations: complete_refining
		# already zeroed the work order's then-existing operations at the correct
		# time, and any weight a pre-refining operation carries now comes from its
		# own real MOP Log history (Employee IR / Stock Entry movements). Blanking
		# only the doc field there would be both wrong (it may be legitimately
		# active/WIP) and futile (the next MOP Log recompute would restore it).
		for mop in post_refining_mops:
			frappe.db.set_value(
				"Manufacturing Operation", mop, zero_map, update_modified=False
			)
		zeroed_mops += len(post_refining_mops)

	print(
		f"zero_post_refining_mop_weights: {len(re_creation_by_mwo)} refined MWO(s), "
		f"zeroed {zeroed_mops} operation(s), cancelled {cancelled_logs} phantom MOP Log row(s)"
	)
	if synced_phantoms:
		print(
			"zero_post_refining_mop_weights: WARNING — "
			f"{len(synced_phantoms)} cancelled phantom row(s) were already EOD-synced "
			"to the physical ledger; run Recalculate Batch Qty / SBB repair on the "
			f"affected operations' warehouses: {sorted(set(synced_phantoms))}"
		)
