// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Warehouse", {
	refresh(frm) {
		// Employee Raw Material (MSL) warehouses carry an item-wise
		// Issue/Receive/Loss/Pending table recomputed from the ledger on demand (req #7).
		let is_msl = !frm.is_new() && frm.doc.employee && frm.doc.warehouse_type === "Raw Material";

		if (is_msl) {
			frm.add_custom_button(__("Refresh Tracking"), () => {
				frappe.call({
					method: "jewellery_erpnext.jewellery_erpnext.doc_events.warehouse_tracking.recalculate_msl_tracking",
					args: { warehouse: frm.doc.name },
					freeze: true,
					freeze_message: __("Recalculating tracking…"),
					callback: (r) => {
						frappe.show_alert({
							message: __("Tracking updated ({0} items)", [r.message || 0]),
							indicator: "green",
						});
						frm.reload_doc();
					},
				});
			});
		}

		// Issue / Receive Material buttons — mirror the Tree Number material flow but keyed on this
		// MSL warehouse: Issue = Dept RM -> here; Receive = here -> Dept RM, difference -> Dept Scrap.
		// Rendered as top-level toolbar buttons (no group) so they're immediately visible.
		if (is_msl && !frm.doc.disabled) {
			frm.add_custom_button(__("Issue Material"), () => issue_material_dialog(frm));
			frm.add_custom_button(__("Receive Material"), () => receive_material_dialog(frm));
		}
	},
});

function issue_material_dialog(frm) {
	frappe.prompt(
		[
			{ fieldtype: "Link", options: "Item", fieldname: "item_code", label: __("Item"), reqd: 1 },
			{ fieldtype: "Float", fieldname: "qty", label: __("Qty"), reqd: 1 },
			{
				fieldtype: "Link",
				options: "Warehouse",
				fieldname: "source_warehouse",
				label: __("Source Warehouse"),
				description: __("Defaults to the linked employee's department Raw Material warehouse."),
			},
		],
		(values) => {
			frappe.call({
				method: "jewellery_erpnext.jewellery_erpnext.doc_events.warehouse_stock_entry.issue_material",
				args: {
					warehouse: frm.doc.name,
					item_code: values.item_code,
					qty: values.qty,
					source_warehouse: values.source_warehouse,
				},
				freeze: true,
				freeze_message: __("Issuing Material..."),
				callback: (r) => {
					if (!r.exc) {
						frappe.show_alert({
							message: __("Issued via Stock Entry {0}", [r.message]),
							indicator: "green",
						});
						frm.reload_doc();
					}
				},
			});
		},
		__("Issue Material"),
		__("Issue")
	);
}

function receive_material_dialog(frm) {
	// Pull live pending (Issue - Receive - Loss) from the ledger so the grid never drifts from
	// the maintained tracking copy.
	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doc_events.warehouse_stock_entry.get_receivable_items",
		args: { warehouse: frm.doc.name },
		freeze: true,
		freeze_message: __("Loading pending items…"),
		callback: (r) => {
			let pending_rows = r.message || [];
			if (!pending_rows.length) {
				frappe.msgprint(__("No pending material to receive."));
				return;
			}

			// Auto-difference model: the operator enters the qty returned to the department RM
			// warehouse; the server books loss = pending - return into the department Scrap
			// warehouse. Only checked ("Settle") rows are processed — this prevents accidentally
			// scrapping every held item. Return Qty defaults to the full pending (return all).
			let grid_fields = [
				{
					fieldtype: "Check",
					fieldname: "settle",
					label: __("Settle"),
					in_list_view: 1,
					columns: 1,
				},
				{
					fieldtype: "Link",
					options: "Item",
					fieldname: "item_code",
					label: __("Item"),
					read_only: 1,
					in_list_view: 1,
					columns: 5,
				},
				{
					fieldtype: "Float",
					fieldname: "pending_qty",
					label: __("Pending"),
					read_only: 1,
					in_list_view: 1,
					columns: 2,
				},
				{
					fieldtype: "Float",
					fieldname: "return_qty",
					label: __("Return Qty"),
					in_list_view: 1,
					columns: 3,
				},
			];

			let d = new frappe.ui.Dialog({
				title: __("Receive Material"),
				size: "large",
				fields: [
					{
						fieldtype: "HTML",
						fieldname: "help",
						options: `<p class="text-muted small">${__(
							"Tick <b>Settle</b> on the items being returned. The difference (Pending − Return Qty) is booked as loss to the department Scrap warehouse."
						)}</p>`,
					},
					{
						fieldtype: "Table",
						fieldname: "rows",
						label: __("Material"),
						cannot_add_rows: true,
						cannot_delete_rows: true,
						in_place_edit: false,
						data: pending_rows.map((row) => ({
							settle: 0,
							item_code: row.item_code,
							pending_qty: row.pending_qty,
							return_qty: row.pending_qty,
						})),
						fields: grid_fields,
					},
				],
				primary_action_label: __("Receive"),
				primary_action: (values) => {
					let rows = (values.rows || [])
						.filter((r) => cint(r.settle))
						.map((r) => ({ item_code: r.item_code, return_qty: flt(r.return_qty) }));
					if (!rows.length) {
						frappe.msgprint(__("Tick Settle on at least one item to receive."));
						return;
					}
					frappe.call({
						method: "jewellery_erpnext.jewellery_erpnext.doc_events.warehouse_stock_entry.receive_material",
						args: { warehouse: frm.doc.name, rows: JSON.stringify(rows) },
						freeze: true,
						freeze_message: __("Receiving Material..."),
						callback: (resp) => {
							if (!resp.exc) {
								d.hide();
								// receive_material returns 1-2 SE names (received transfer + loss Repack).
								let names = Array.isArray(resp.message)
									? resp.message.join(", ")
									: resp.message;
								frappe.show_alert({
									message: __("Received via Stock Entry(s) {0}", [names]),
									indicator: "green",
								});
								frm.reload_doc();
							}
						},
					});
				},
			});
			d.show();
		},
	});
}
