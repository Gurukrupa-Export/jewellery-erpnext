// Stock Reconciliation enhancements:
// - live Item Group summary, pure weight and positive / negative difference totals. The server
//   recomputes the same values on save (stock_reonciliation.set_reconciliation_summaries), so these
//   are display-only and never the source of truth;
// - "Fetch Items from Warehouse" with several items (or all of them) at once.

const SR_METHODS =
	"jewellery_erpnext.jewellery_erpnext.customization.stock_reconciliation.stock_reonciliation";
const SR_PRECISION = 3;
// Stock UOM -> grams. carat / 5 = gram (carat x 0.2). Other UOMs have no gram value.
const SR_GRAM_FACTORS = { Gram: 1, Carat: 0.2 };

frappe.ui.form.on("Stock Reconciliation", {
	setup(frm) {
		// ERPNext's "Fetch Items from Warehouse" button calls frm.events.get_items.
		frm.events.get_items = (frm) => sr_get_items_dialog(frm);
	},

	// Also triggered by the "Stock Reconciliation Get work Order" Client Script once it has
	// filled Stock Comparison.
	jewellery_recalculate_summaries(frm) {
		sr_recalculate(frm);
	},
});

frappe.ui.form.on("Stock Reconciliation Item", {
	item_code: sr_schedule_recalculate,
	qty: sr_schedule_recalculate,
	item_group: sr_schedule_recalculate,
	stock_uom: sr_schedule_recalculate,
	items_remove: sr_schedule_recalculate,
});

frappe.ui.form.on("Stock Comparison", {
	qty: sr_schedule_recalculate,
	currenty_qty: sr_schedule_recalculate,
	custom_stock_comparison_remove: sr_schedule_recalculate,
});

function sr_schedule_recalculate(frm) {
	// Several fields change together (item_code fetches item_group and stock_uom): recalculate once.
	clearTimeout(frm._sr_recalculate_timer);
	frm._sr_recalculate_timer = setTimeout(() => sr_recalculate(frm), 300);
}

async function sr_purities(frm, item_codes) {
	frm._sr_purity_cache = frm._sr_purity_cache || {};
	const cache = frm._sr_purity_cache;
	const missing = [...new Set(item_codes.filter((code) => code && !(code in cache)))];
	if (missing.length) {
		const found = await frappe.xcall(`${SR_METHODS}.get_purity_percentages`, {
			item_codes: missing,
		});
		missing.forEach((code) => (cache[code] = flt((found || {})[code])));
	}
	return cache;
}

function sr_pure_weight(qty, purity) {
	return purity ? flt((flt(qty) * flt(purity)) / 100, SR_PRECISION) : 0;
}

async function sr_recalculate(frm) {
	if (frm.doc.docstatus !== 0) return;
	const items = frm.doc.items || [];
	const comparison = frm.doc.custom_stock_comparison || [];
	const purity = await sr_purities(frm, [
		...items.map((row) => row.item_code),
		...comparison.map((row) => row.item_code),
	]);

	// Pure weight of each item row and the total
	if (frappe.meta.has_field("Stock Reconciliation Item", "custom_pure_weight")) {
		let total = 0;
		items.forEach((row) => {
			row.custom_pure_weight = sr_pure_weight(row.qty, purity[row.item_code]);
			total += row.custom_pure_weight;
		});
		if (frappe.meta.has_field(frm.doctype, "custom_total_pure_weight")) {
			frm.doc.custom_total_pure_weight = flt(total, SR_PRECISION);
		}
	}

	// Item Group summary
	if (frappe.meta.has_field(frm.doctype, "custom_item_group_summary")) {
		const totals = {};
		items.forEach((row) => {
			if (!row.item_code) return;
			const key = JSON.stringify([row.item_group || "", row.stock_uom || ""]);
			totals[key] = (totals[key] || 0) + flt(row.qty);
		});
		frm.clear_table("custom_item_group_summary");
		let grand_total = 0;
		Object.keys(totals)
			.sort()
			.forEach((key) => {
				const [item_group, uom] = JSON.parse(key);
				const factor = SR_GRAM_FACTORS[uom];
				const in_gram = factor ? flt(totals[key] * factor, SR_PRECISION) : 0;
				grand_total += in_gram;
				const row = frm.add_child("custom_item_group_summary");
				row.item_group = item_group || null;
				row.uom = uom || null;
				row.total_qty = flt(totals[key], SR_PRECISION);
				row.total_qty_in_gram = in_gram;
			});
		frm.doc.custom_grand_total_gram = flt(grand_total, SR_PRECISION);
	}

	// Signed difference (Stock Reconciliation qty - Child Stock Reconciliation qty) and its totals
	let positive = 0;
	let negative = 0;
	const comparison_pure_weight = frappe.meta.has_field("Stock Comparison", "pure_weight");
	comparison.forEach((row) => {
		row.diff_qty = flt(flt(row.currenty_qty) - flt(row.qty), SR_PRECISION);
		if (row.diff_qty > 0) positive += row.diff_qty;
		if (row.diff_qty < 0) negative -= row.diff_qty;
		if (comparison_pure_weight) {
			row.pure_weight = sr_pure_weight(row.diff_qty, purity[row.item_code]);
		}
	});
	if (frappe.meta.has_field(frm.doctype, "custom_positive_difference_qty")) {
		frm.doc.custom_positive_difference_qty = flt(positive, SR_PRECISION);
		frm.doc.custom_negative_difference_qty = flt(negative, SR_PRECISION);
	}

	frm.refresh_fields([
		"items",
		"custom_item_group_summary",
		"custom_grand_total_gram",
		"custom_total_pure_weight",
		"custom_stock_comparison",
		"custom_positive_difference_qty",
		"custom_negative_difference_qty",
	]);
}

function sr_warehouse_filters(frm) {
	// Same warehouse scope as the earlier single-item dialog.
	const filters = { company: frm.doc.company };
	if (frm.doc.custom_main_slip == 1) {
		filters.custom_parent_department_warehouse = frm.doc.custom_department;
	} else {
		filters.department = frm.doc.custom_department;
		filters.warehouse_type = ["in", ["Raw Material", "Reserve", "Scrap"]];
	}
	return filters;
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
				get_query: () => ({ filters: sr_warehouse_filters(frm) }),
				onchange() {
					dialog.set_value("item_codes", []);
				},
			},
			{
				label: __("Select All Items"),
				fieldname: "select_all",
				fieldtype: "Check",
				description: __("Fetch every item in the warehouse"),
			},
			{
				label: __("Items"),
				fieldname: "item_codes",
				fieldtype: "MultiSelectList",
				depends_on: "eval:!doc.select_all",
				get_data(txt) {
					const warehouse = dialog.get_value("warehouse");
					if (!warehouse) return [];
					return frappe.xcall(`${SR_METHODS}.get_warehouse_items`, { warehouse, txt });
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
	sr_recalculate(frm);
	frappe.show_alert({
		message: __("{0} item(s) added, {1} already in the table", [added, skipped]),
		indicator: added ? "green" : "orange",
	});
}
