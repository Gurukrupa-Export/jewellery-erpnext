# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from jewellery_erpnext.jewellery_erpnext.doctype.tree_number import (
	tree_material_balance as tree_balance,
)
from jewellery_erpnext.jewellery_erpnext.doctype.tree_number.tree_utils import (
	get_computed_gold_wt,
	get_flask_weights,
)


class TreeNumber(Document):
	def validate(self):
		# Snapshot the persisted violations BEFORE recomputing, so validate_row_balance can tell
		# a pre-existing over-draw (historical data, must stay openable) from a new one.
		previous_violations = tree_balance.stored_row_violations(self)
		self.calculate_tree_details()
		self.calculate_flask_details()
		self.calculate_material_pending()
		tree_balance.validate_row_balance(self, previous_violations=previous_violations)

	def calculate_tree_details(self):
		"""Wax tree weight -> computed gold weight using the KT conversion factor."""
		self.computed_gold_wt = get_computed_gold_wt(
			self.manufacturer, self.metal_touch, self.tree_wax_wt
		)

	def calculate_flask_details(self):
		"""Powder weight -> water / boric / special powder weights."""
		weights = get_flask_weights(
			self.manufacturer, self.powder_wt, self.is_wax_setting
		)
		self.water_weight = weights["water_weight"]
		if self.is_wax_setting:
			self.boric_powder_weight = weights["boric_powder_weight"]
			self.special_powder_weight = weights["special_powder_weight"]
		else:
			self.boric_powder_weight = 0
			self.special_powder_weight = 0

	def calculate_material_pending(self):
		"""Pending Qty = Issue Qty - Receive Qty - Loss Qty per material row.

		THE single writer of ``pending_qty`` — ``validate`` runs on every save, so whatever the
		Issue/Receive paths compute is re-derived here. Keeping one writer is what stops the four
		call paths drifting apart again.

		Deliberately UNFLOORED for casting trees too. The old floor was there to hide the negative
		left by a receive booked against a tree that was never issued; now that the receive itself
		is capped at what the tree holds, a negative can only mean historical over-draw — which
		must stay visible for the audit rather than being silently clamped to zero.
		"""
		precision = tree_balance.qty_precision()
		for row in self.material_details:
			tree_balance.recompute_row_pending(row, precision)

	def after_insert(self):
		counter = cint(frappe.db.sql("select max(counter) from `tabTree Number`")[0][0])
		self.db_set("counter", counter + 1)

	@frappe.whitelist()
	def issue_material(self, item_code, qty, source_warehouse=None):
		"""Issue material into this (standalone) tree's MSL warehouse via a Material Transfer SE."""
		from jewellery_erpnext.jewellery_erpnext.doctype.tree_number.doc_events.tree_stock_entry import (
			issue_material as _issue_material,
		)

		return _issue_material(self, item_code, qty, source_warehouse)

	@frappe.whitelist()
	def receive_material(self, rows):
		"""Receive / book loss for this tree via a Material Transfer SE."""
		from jewellery_erpnext.jewellery_erpnext.doctype.tree_number.doc_events.tree_stock_entry import (
			receive_material as _receive_material,
		)

		return _receive_material(self, rows)

	@frappe.whitelist()
	def submit_tree(self):
		"""Finalize a received tree: write off any remaining pending as loss, then lock it at
		'Submitted' (no further Issue/Receive).

		Allowed once the tree has had SOME receive activity — status 'Received' OR 'Partially
		Received'. A never-received tree ('Issued'/'Draft') cannot be submitted (reverse or receive
		it first). When the tree is only Partially Received, the leftover pending on each row is
		booked as loss to the department Scrap warehouse — a 'Process Loss' entry that mirrors the
		Receive button's loss leg (consume metal @ MSL, produce the ML variant @ Scrap) — so the
		ledger balances to pending 0 before the tree locks. 'Submitted' is a manual, terminal,
		code-driven state; once set, issue_material / receive_material / update_tree_on_receive all
		refuse to touch the tree.
		"""
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.tree_casting import (
			lock_tree,
		)

		frappe.has_permission("Tree Number", "write", self, throw=True)
		# Parent control row first (lock_order position 1) — receive_material below takes Series
		# and Bin locks, so the tree must already be held before it runs.
		lock_tree(self.name)
		if tree_balance.tree_status(self) not in ("Received", "Partially Received"):
			frappe.throw(
				_(
					"Tree {0} can be submitted only after material has been received "
					"(status Received or Partially Received)."
				).format(self.name)
			)

		eps = tree_balance.pending_eps()

		# Re-derive pending from the quantities before judging it. The stored column cannot be
		# trusted here: rows written before the floor was removed persisted pending 0 on a ledger
		# that is really over-drawn, so reading it straight would wave those trees through.
		self.calculate_material_pending()

		# An over-drawn row cannot be written off — the leftover selection below only picks up
		# POSITIVE pending, so a negative one would slip through and the tree would lock with a
		# permanently broken ledger and no correction path.
		over_drawn = [md for md in self.material_details if flt(md.pending_qty) < -eps]
		if over_drawn:
			frappe.throw(
				_(
					"Tree {0} cannot be submitted: {1} has been received/lost beyond what was "
					"issued (pending {2}). Reconcile the ledger before locking the tree."
				).format(
					self.name,
					", ".join(md.item_code for md in over_drawn),
					", ".join(str(flt(md.pending_qty)) for md in over_drawn),
				),
				title=_("Tree Ledger Over-Drawn"),
			)

		# Write off any remaining pending (above the dust tolerance) as loss so the ledger balances
		# to pending 0 before the tree locks. Reuses receive_material's loss leg (Process Loss SE +
		# ledger update + canonical lock ordering); it saves the tree with the updated ledger/status.
		leftover = [
			{"item_code": md.item_code, "loss_qty": flt(md.pending_qty)}
			for md in self.material_details
			if flt(md.pending_qty) > eps
		]
		if leftover:
			from jewellery_erpnext.jewellery_erpnext.doctype.tree_number.doc_events.tree_stock_entry import (
				receive_material as _receive_material,
			)

			_receive_material(self, leftover)

		self.status = "Submitted"
		self.save(ignore_permissions=True)

	@frappe.whitelist()
	def reverse_tree_stock_entries(self):
		"""Cancel the tree's Issue SEs + reset the ledger (unwind a mis-issue). Only allowed
		BEFORE any receive activity — mirrors unlink_tree_on_issue_cancel. This blocks reversing
		a tree whose casting EIR already physically received (which would cancel the Source->MSL
		SE against a drained MSL -> negative stock) or desync a received tree from a live EIR."""
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.tree_casting import (
			lock_tree,
		)

		frappe.has_permission("Tree Number", "write", self, throw=True)
		lock_tree(self.name)
		if any(
			flt(r.receive_qty) or flt(r.loss_qty) for r in self.material_details
		) or (self.status in ("Partially Received", "Received", "Submitted")):
			frappe.throw(
				_(
					"Cannot reverse Tree {0}: material has already been received. Cancel the "
					"receive Employee IR(s) first."
				).format(self.name)
			)
		from jewellery_erpnext.jewellery_erpnext.doctype.tree_number.doc_events.tree_stock_entry import (
			cancel_tree_stock_entries,
		)

		cancelled = cancel_tree_stock_entries(self)
		# Zero the issue ledger so the tree can be re-issued cleanly. Status is then derived, not
		# asserted: an emptied ledger is "Draft", never "Issued" — nothing is on the tree any more.
		for md in self.material_details:
			md.issue_qty = 0
			md.pending_qty = 0
		self.status = tree_balance.tree_status(self)
		self.save(ignore_permissions=True)
		return cancelled
