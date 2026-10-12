// Copyright (c) 2026, Nirali and contributors
// For license information, please see license.txt

// Batch form Repack (Actions > Repack). Whether the button shows, and which warehouses the
// dialog offers, is decided on the server by get_batch_repack_context: the batch's company is
// resolved there, so this file never reads frm.doc.custom_company. The server re-checks every
// value when the entry is created.

const BATCH_REPACK_METHODS = "jewellery_erpnext.jewellery_erpnext.customization.batch.repack";

frappe.ui.form.on("Batch", {
	refresh(frm) {
		if (frm.is_new()) return;

		const batch_no = frm.doc.name;
		frappe.call({
			method: `${BATCH_REPACK_METHODS}.get_batch_repack_context`,
			args: { batch_no },
			callback: (r) => {
				const ctx = r.message;
				// The form may have moved to another batch while the call was in flight.
				if (!ctx || !ctx.allowed || frm.doc.name !== batch_no) return;
				if (!(ctx.warehouses || []).length) return;

				frm.add_custom_button(__("Repack"), () => make_repack_dialog(frm, ctx), __("Actions"));
			},
		});
	},
});

function make_repack_dialog(frm, ctx) {
	const warehouses = ctx.warehouses || [];
	const available_qty = (warehouse) => {
		const row = warehouses.find((w) => w.warehouse === warehouse);
		return row ? flt(row.qty) : 0;
	};
	const first_warehouse = warehouses[0].warehouse;
	// Declared up front: the warehouse onchange can fire while the dialog is being built.
	let dialog = null;

	const fields = [
		{
			fieldname: "warehouse",
			fieldtype: "Select",
			label: __("Warehouse"),
			options: warehouses.map((w) => w.warehouse),
			default: first_warehouse,
			reqd: 1,
			onchange() {
				if (!dialog) return;
				const qty = available_qty(dialog.get_value("warehouse"));
				dialog.set_value("available_qty", qty);
				dialog.set_value("qty", qty);
			},
		},
		{
			fieldname: "available_qty",
			fieldtype: "Float",
			label: __("Available Qty"),
			read_only: 1,
			default: available_qty(first_warehouse),
		},
		{
			fieldname: "qty",
			fieldtype: "Float",
			label: __("Qty"),
			reqd: 1,
			default: available_qty(first_warehouse),
		},
		{
			fieldname: "target_rate",
			fieldtype: "Currency",
			label: __("Target Rate"),
			reqd: 1,
			description: __("Valuation rate of the new (target) batch"),
		},
		{
			fieldname: "posting_datetime",
			fieldtype: "Datetime",
			label: __("Posting Date & Time"),
			reqd: 1,
			default: frappe.datetime.now_datetime(),
		},
	];

	if (ctx.branch) {
		fields.push({
			fieldname: "branch",
			fieldtype: "Link",
			options: ctx.branch.document_type,
			label: __(ctx.branch.label),
			reqd: ctx.branch.reqd ? 1 : 0,
			default: ctx.branch.default,
		});
	}

	dialog = new frappe.ui.Dialog({
		title: __("Create Repack Entry"),
		fields,
		primary_action_label: __("Submit"),
		primary_action(values) {
			const available = available_qty(values.warehouse);
			if (flt(values.qty) <= 0 || flt(values.qty) > available) {
				frappe.msgprint(__("Qty must be greater than zero and at most {0}.", [available]));
				return;
			}
			if (flt(values.target_rate) <= 0) {
				frappe.msgprint(__("Target Rate must be greater than zero."));
				return;
			}

			dialog.hide();
			const escape = (value) => frappe.utils.escape_html(String(value));
			frappe.confirm(
				__("Create a Repack Stock Entry for {0} of batch {1} from warehouse {2} at rate {3}?", [
					`<b>${escape(values.qty)}</b>`,
					`<b>${escape(frm.doc.name)}</b>`,
					`<b>${escape(values.warehouse)}</b>`,
					`<b>${escape(format_currency(values.target_rate))}</b>`,
				]),
				() => create_repack(frm, values)
			);
		},
	});

	dialog.show();
}

function create_repack(frm, values) {
	frappe.call({
		method: `${BATCH_REPACK_METHODS}.create_repack_stock_entry`,
		type: "POST",
		args: {
			batch_no: frm.doc.name,
			warehouse: values.warehouse,
			qty: values.qty,
			target_rate: values.target_rate,
			posting_datetime: values.posting_datetime,
			branch: values.branch || null,
		},
		freeze: true,
		freeze_message: __("Creating Repack Entry..."),
		callback: (r) => {
			if (!r.message) return;

			const { stock_entry, journal_entry } = r.message;
			let message = __("Stock Entry {0} created and submitted.", [
				frappe.utils.get_form_link("Stock Entry", stock_entry, true),
			]);
			if (journal_entry) {
				message +=
					"<br>" +
					__("Journal Entry {0} created for the value difference.", [
						frappe.utils.get_form_link("Journal Entry", journal_entry, true),
					]);
			}

			frappe.msgprint({
				title: __("Repack Completed"),
				message,
				indicator: "green",
			});
			frm.reload_doc();
		},
	});
}
