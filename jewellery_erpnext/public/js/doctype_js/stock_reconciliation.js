// Stock Reconciliation:
// - "Get Child Stock Reconcilliation" button;
// - "Fetch Items from Warehouse" with several items (or all of them) at once. With Main Slip
//   ticked it also fetches the employee warehouses of the selected warehouse.

const SR_METHODS =
	"jewellery_erpnext.jewellery_erpnext.customization.stock_reconciliation.stock_reonciliation";

frappe.ui.form.on("Stock Reconciliation", {
	setup(frm) {
		// ERPNext's "Fetch Items from Warehouse" button calls frm.events.get_items.
		frm.events.get_items = (frm) => sr_get_items_dialog(frm);
	},

	custom_get_child_stock_reconcilliation(frm) {
		frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.customization.stock_reconciliation.stock_reonciliation.get_child_reconciliation",
			args: {
				doc: frm.doc.name,
			},
			callback: function (r) {
				$.each(r.message, function (i, item) {
					// Check if item already exists in the table
					var existing_item = false;
					frm.doc.items.forEach(function (existing_row) {
						if (existing_row.item_code === item.item_code) {
							existing_item = true;
							return false; // exit loop early
						}
					});
					// Add item if it doesn't exist already
					if (!existing_item) {
						var row = frappe.model.add_child(frm.doc, "Stock Reconciliation Item", "items");
						row.item_code = item.item_code;
						row.warehouse = item.warehouse;
						row.qty = item.qty;
						row.valuation_rate = item.valuation_rate;
					}
				});
				refresh_field("items");
			},
		});
	},
});

function sr_main_slip(frm) {
	// The form's "Main Slip" box (fieldname custom_main_screen, from gke_customization): when
	// ticked, Get Items also fetches the selected warehouse's employee warehouses.
	return cint(frm.doc.custom_main_screen) ? 1 : 0;
}

function sr_get_items_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Get Items"),
		fields: [
			{
				label: __("Warehouse"),
				fieldname: "warehouse",
				fieldtype: "Link",
				options: "Warehouse",
				reqd: 1,
				// Same scope as ERPNext's dialog.
				get_query: () => ({ filters: { company: frm.doc.company } }),
				onchange() {
					dialog.set_value("item_codes", []);
				},
			},
			{
				label: __("Select All Items"),
				fieldname: "select_all",
				fieldtype: "Check",
				description: __(
					"Fetch every item in the warehouse (and its employee warehouses when Main Slip is ticked)"
				),
			},
			{
				label: __("Items"),
				fieldname: "item_codes",
				fieldtype: "MultiSelectList",
				depends_on: "eval:!doc.select_all",
				get_data(txt) {
					const warehouse = dialog.get_value("warehouse");
					if (!warehouse) return [];
					return frappe.xcall(`${SR_METHODS}.get_warehouse_items`, {
						warehouse,
						txt,
						main_slip: sr_main_slip(frm),
					});
				},
			},
			{
				label: __("Ignore Empty Stock"),
				fieldname: "ignore_empty_stock",
				fieldtype: "Check",
			},
		],
		primary_action_label: __("Update"),
		async primary_action(values) {
			const item_codes = values.item_codes || [];
			if (!values.select_all && !item_codes.length) {
				frappe.msgprint(__("Select at least one item, or tick Select All Items."));
				return;
			}
			const rows = await frappe.xcall(`${SR_METHODS}.get_items_for_reconciliation`, {
				warehouse: values.warehouse,
				posting_date: frm.doc.posting_date,
				posting_time: frm.doc.posting_time,
				company: frm.doc.company,
				item_codes,
				select_all: values.select_all ? 1 : 0,
				ignore_empty_stock: values.ignore_empty_stock ? 1 : 0,
				main_slip: sr_main_slip(frm),
			});
			dialog.hide();
			sr_add_fetched_items(frm, rows || []);
		},
	});
	dialog.show();
}

function sr_add_fetched_items(frm, rows) {
	const key = (row) => [row.item_code, row.warehouse, row.batch_no || ""].join("::");

	// Drop the empty starter row (no item and no work order) before adding.
	const blank = (frm.doc.items || []).filter(
		(row) => !row.item_code && !row.custom_manufacturing_work_order
	);
	blank.forEach((row) => frappe.model.clear_doc(row.doctype, row.name));

	const existing = new Set((frm.doc.items || []).map(key));
	let added = 0;
	let skipped = 0;
	rows.forEach((row) => {
		if (existing.has(key(row))) {
			skipped++;
			return;
		}
		existing.add(key(row));
		const item = frm.add_child("items");
		$.extend(item, row);
		item.qty = item.qty || 0;
		item.valuation_rate = item.valuation_rate || 0;
		item.use_serial_batch_fields = cint(frappe.user_defaults?.use_serial_batch_fields);
		added++;
	});

	frm.refresh_field("items");
	frappe.show_alert({
		message: __("{0} item(s) added, {1} already in the table", [added, skipped]),
		indicator: added ? "green" : "orange",
	});
}
