import frappe
from erpnext.stock.doctype.stock_reconciliation.stock_reconciliation import (
	EmptyStockReconciliationItemsError,
	StockReconciliation,
)
from frappe import _
from frappe.utils import flt


@frappe.whitelist()
def get_child_reconciliation(doc, method=None):
	# frappe.throw(f"{doc}")
	child_stock = frappe.db.get_all(
		"Child Stock Reconcilation", {"stock_reconcillation": doc}, ["name"]
	)
	items = []
	for stock in child_stock:
		child_items = frappe.get_all(
			"Child Stock Reconcilation Item",
			filters={"parent": stock.name},
			fields=["*"],
		)
		for item in child_items:
			if item.item_code is not None:
				items.append(
					{
						"item_code": item.item_code,
						"warehouse": item.warehouse,
						"qty": item.qty,
						"valuation_rate": item.valuation_rate,
					}
				)

	return items


def validate_department(self, method=None):
	if not self.set_warehouse:
		return

	if self.workflow_state not in ["In Progress", "Send for Approval"]:
		return

	department = frappe.db.get_value("Warehoouse", self.set_warehouse, "department")

	if not department:
		frappe.msgprint(_("Department not mentioned in warehouse"))
		return

	if frappe.db.get_all(
		"Manufacturig Operation",
		{"department": department, "department_ir_status": "In-Transit"},
	):
		frappe.throw(
			_(
				"Some Manufacturing Operations are in Transit mode, Complete Transit First then perform the action"
			)
		)


def validate_transit_warehouse_empty(self, method=None):
	"""Block save/submit unless every reconciled item has zero stock in the department's
	Transit warehouse.

	Reconciling a department while items still sit in its transit warehouse (mid-transfer)
	yields wrong balances. So: resolve the department from ``custom_department``, find its
	Transit warehouse (``Warehouse{department, warehouse_type="Transit", disabled=0}``), and
	require ``Bin.actual_qty`` there to be 0 for every distinct item on the reconciliation.

	``custom_department`` is read via ``.get()`` (it is a gke_customization fixture field and
	may be absent on un-synced sites) so a missing field degrades to "skip" rather than raising.
	A missing Bin row means no stock for that (item, warehouse) pair, i.e. 0 -- handled by
	``flt(None) == 0.0`` and ``on_hand.get(ic, 0.0)``.
	"""
	department = self.get("custom_department")
	if not department:
		return

	transit_warehouse = frappe.db.get_value(
		"Warehouse",
		{"department": department, "warehouse_type": "Transit", "disabled": 0},
		"name",
	)
	if not transit_warehouse:
		return  # department has no transit warehouse -> nothing can be in transit

	item_codes = {row.item_code for row in self.items if row.item_code}
	if not item_codes:
		return

	# One bulk read (matches the app's Bin bulk-prefetch idiom, main_slip_inject.py:858).
	on_hand = {
		b["item_code"]: flt(b["actual_qty"])
		for b in frappe.db.get_all(
			"Bin",
			filters={
				"warehouse": transit_warehouse,
				"item_code": ["in", list(item_codes)],
			},
			fields=["item_code", "actual_qty"],
		)
	}

	blocked = sorted(ic for ic in item_codes if on_hand.get(ic, 0.0) != 0)
	if blocked:
		rows = "".join(f"<li>{ic}: {on_hand[ic]}</li>" for ic in blocked)
		frappe.throw(
			_(
				"Stock Reconciliation cannot be saved: the following item(s) still have stock "
				"in the transit warehouse <b>{0}</b>. Clear the transit warehouse first."
			).format(transit_warehouse)
			+ f"<ul>{rows}</ul>",
			title=_("Transit Warehouse Not Empty"),
		)


class CustomStockReconciliation(StockReconciliation):
	def has_custom_mwo(self):
		"""Return True if any row has custom_manufacturing_work_order checked.

		Read via ``.get()`` (not raw attribute access): ``custom_manufacturing_work_order``
		is a gke_customization fixture field on Stock Reconciliation Item and is absent
		on sites where that fixture hasn't been synced, where ``row.custom_manufacturing_work_order``
		raises AttributeError and aborts every Stock Reconciliation save. ``.get()``
		returns None when the field is missing, so the flow degrades to standard ERPNext
		validation.
		"""
		for row in self.items:
			if row.get("custom_manufacturing_work_order"):
				return True
		return False

	def validate(self):
		# If condition is not met, run standard ERPNext validate
		if not self.has_custom_mwo():
			return super().validate()

		# If condition is met, allow saving draft without item_code
		if self.docstatus == 0:
			return

		# If not draft, run standard validations
		return super().validate()

	def on_submit(self):
		# If condition is not met, run standard ERPNext submit
		if not self.has_custom_mwo():
			return super().on_submit()

		# If condition is met, block submit if item_code missing
		for row in self.items:
			if not row.item_code:
				frappe.throw(
					_("You cannot submit Stock Reconciliation without Item Code.")
				)

		return super().on_submit()

	def remove_items_with_no_change(self):
		"""Keep every row in the MWO / auto-creation lanes, but still compute totals.

		This used to be a bare ``return``, which looked like it only disabled row
		trimming. It did far more than that: upstream's implementation
		(erpnext/stock/doctype/stock_reconciliation/stock_reconciliation.py) is the
		ONLY place that

		  * initialises and accumulates ``self.difference_amount``,
		  * back-fills ``item.current_qty`` / ``item.current_valuation_rate``,
		  * raises ``EmptyStockReconciliationItemsError`` for a no-op reconciliation.

		Skipping it therefore left ``difference_amount`` at whatever the form sent —
		measured on the production dataset: 27 of 29 submitted Stock Reconciliations
		carried 0, and 922 of 2,450 submitted item rows carried a zero
		``current_valuation_rate``.

		So delegate to ``super()`` unconditionally, and undo only the row trimming in
		the lanes that need every row preserved. Upstream throws AFTER accumulating
		``difference_amount`` and BEFORE mutating ``self.items``, so catching the
		empty-items error is safe and still leaves the totals computed.
		"""
		keep_all_rows = self.has_custom_mwo() or bool(self.get("custom_auto_creation"))

		if not keep_all_rows:
			return super().remove_items_with_no_change()

		original_items = list(self.items)
		try:
			super().remove_items_with_no_change()
		except EmptyStockReconciliationItemsError:
			# A reconciliation whose rows all match current stock is legitimate in
			# these lanes: the rows are the MWO's items and must survive regardless.
			pass
		finally:
			if len(self.items) != len(original_items):
				self.items = original_items
				for idx, row in enumerate(self.items, start=1):
					row.idx = idx
