# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from jewellery_erpnext.jewellery_erpnext.doctype.tree_number.tree_utils import (
	get_computed_gold_wt,
	get_flask_weights,
)


class TreeNumber(Document):
	def validate(self):
		self.calculate_tree_details()
		self.calculate_flask_details()
		self.calculate_material_pending()

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

		Floating-point dust (e.g. 3 - 2.9 - 0.1 ≈ 8e-17) is tolerated downstream by the eps
		guard in _tree_status and the receive caps, so pending is left unrounded here.

		Casting trees (``employee_ir`` set) keep ``issue_qty`` button-owned but the Employee IR
		Receive draws the committed metal from the tree even when it was never button-issued, so
		``pending`` floors at 0 (a never-issued row reads 0, not a phantom negative). Standalone
		trees keep the raw value — their button paths cap receive at pending, so a negative there
		signals a real bug that must stay visible.
		"""
		is_casting = bool(self.get("employee_ir"))
		for row in self.material_details:
			pending = flt(row.issue_qty) - flt(row.receive_qty) - flt(row.loss_qty)
			row.pending_qty = max(0.0, pending) if is_casting else pending

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
			_pending_eps,
			_tree_status,
		)

		frappe.has_permission("Tree Number", "write", self, throw=True)
		if _tree_status(self) not in ("Received", "Partially Received"):
			frappe.throw(
				_(
					"Tree {0} can be submitted only after material has been received "
					"(status Received or Partially Received)."
				).format(self.name)
			)

		# Write off any remaining pending (above the dust tolerance) as loss so the ledger balances
		# to pending 0 before the tree locks. Reuses receive_material's loss leg (Process Loss SE +
		# ledger update + canonical lock ordering); it saves the tree with the updated ledger/status.
		eps = _pending_eps()
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
		frappe.has_permission("Tree Number", "write", self, throw=True)
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
		# Zero the issue ledger + reset status so the tree can be re-issued cleanly.
		for md in self.material_details:
			md.issue_qty = 0
			md.pending_qty = 0
		self.status = "Issued" if self.material_details else "Draft"
		self.save(ignore_permissions=True)
		return cancelled
