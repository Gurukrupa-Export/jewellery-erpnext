// Copyright (c) 2023, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Manufacturing Plan", {
	refresh(frm) {
		if (frm.doc.docstatus == 1) frm.trigger("show_progress");
		frm.set_query("setting_type", function (doc) {
			return {
				query: "jewellery_erpnext.query.item_attribute_query",
				filters: {
					item_attribute: "Setting Type",
				},
			};
		});
	},
	setup(frm) {
		var parent_fields = [["diamond_quality", "Diamond Quality"]];
		set_item_attribute_filters_on_fields_in_child_doctype(frm, parent_fields);
	},
	get_sales_order(frm) {
		map_current_doc({
			method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan.get_sales_order",
			source_doctype: "Sales Order",
			target: frm,
			setters: {
				customer: null,
				transaction_date: null,
				company: frm.doc.company,
				branch: frm.doc.branch,
			},
			get_query_method:
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan.get_pending_ppo_sales_order",
			size: "extra-large",
		});
	},
	get_mwo(frm) {
		map_current_doc({
			method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.doc_events.utils.get_mwo",
			source_doctype: "Manufacturing Work Order",
			target: frm,
			setters: {
				customer: null,
				company: frm.doc.company,
				branch: frm.doc.branch,
			},
			get_query_filters: {
				company: frm.doc.company,
			},
			get_query_method:
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.doc_events.utils.get_mwo_details",
			// args: { company: frm.doc.company },
			size: "extra-large",
		});
	},
	get_repair_order(frm) {
		map_current_doc({
			method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan.get_sales_order",
			source_doctype: "Sales Order",
			target: frm,
			setters: {
				customer: null,
				transaction_date: null,
				company: frm.doc.company,
				branch: frm.doc.branch,
			},
			get_query_method:
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan.get_repair_pending_ppo_sales_order",
			size: "extra-large",
		});
	},
	show_progress(frm) {
		var bars = [];
		var message = "";
		var title = "";

		// produced qty
		let item_wise_qty = {};
		frm.doc.manufacturing_plan_table.forEach((data) => {
			if (!item_wise_qty[data.item_code]) {
				item_wise_qty[data.item_code] = data.produced_qty;
			} else {
				item_wise_qty[data.item_code] += data.produced_qty;
			}
		});

		if (item_wise_qty) {
			for (var key in item_wise_qty) {
				title += __("Item {0}: {1} qty produced. ", [key, item_wise_qty[key]]);
			}
		}

		bars.push({
			title: title,
			width: (frm.doc.total_produced_qty / frm.doc.total_planned_qty) * 100 + "%",
			progress_class: "progress-bar-success",
		});
		if (bars[0].width == "0%") {
			bars[0].width = "0.5%";
		}
		message = title;
		frm.dashboard.add_progress(__("Status"), bars, message);
	},
});

var map_current_doc = function (opts) {
	function _map(frm) {
		if ($.isArray(frm.doc.items) && frm.doc.items.length > 0) {
			// remove first item row if empty
			if (!frm.doc.items[0].item_code) {
				frm.doc.items = frm.doc.items.splice(1);
			}

			// find the doctype of the items table
			var items_doctype = frappe.meta.get_docfield(frm.doctype, "items").options;

			// find the link fieldname from items table for the given
			// source_doctype
			var link_fieldname = null;
			frappe.get_meta(items_doctype).fields.forEach(function (d) {
				if (d.options === opts.source_doctype) link_fieldname = d.fieldname;
			});

			// search in existing items if the source_name is already set and full qty fetched
			var already_set = false;
			var item_qty_map = {};

			$.each(frm.doc.items, function (i, d) {
				opts.source_name.forEach(function (src) {
					if (d[link_fieldname] == src) {
						already_set = true;
						if (item_qty_map[d.item_code]) item_qty_map[d.item_code] += flt(d.qty);
						else item_qty_map[d.item_code] = flt(d.qty);
					}
				});
			});

			if (already_set) {
				opts.source_name.forEach(function (src) {
					frappe.model.with_doc(opts.source_doctype, src, function (r) {
						var source_doc = frappe.model.get_doc(opts.source_doctype, src);
						$.each(source_doc.items || [], function (i, row) {
							if (row.qty > flt(item_qty_map[row.item_code])) {
								already_set = false;
								return false;
							}
						});
					});

					if (already_set) {
						frappe.msgprint(
							__("You have already selected items from {0} {1}", [
								opts.source_doctype,
								src,
							])
						);
						return;
					}
				});
			}
		}

		return frappe.call({
			// Sometimes we hit the limit for URL length of a GET request
			// as we send the full target_doc. Hence this is a POST request.
			type: "POST",
			method: "frappe.model.mapper.map_docs",
			args: {
				method: opts.method,
				source_names: opts.source_name,
				target_doc: frm.doc,
				args: opts.args,
			},
			callback: function (r) {
				if (!r.exc) {
					var doc = frappe.model.sync(r.message);
					frm.dirty();
					frm.refresh();
				}
			},
		});
	}

	let query_args = {};
	if (opts.get_query_filters) {
		query_args.filters = opts.get_query_filters;
	}

	if (opts.get_query_method) {
		query_args.query = opts.get_query_method;
	}

	if (query_args.filters || query_args.query) {
		opts.get_query = () => query_args;
	}

	if (opts.source_doctype) {
		const d = new frappe.ui.form.MultiSelectDialog({
			doctype: opts.source_doctype,
			target: opts.target,
			date_field: opts.date_field || undefined,
			setters: opts.setters,
			get_query: opts.get_query,
			allow_child_item_selection: opts.allow_child_item_selection,
			child_fieldname: opts.child_fieldname,
			child_columns: opts.child_columns,
			size: opts.size,
			action: function (selections, args) {
				let values = selections;
				if (values.length === 0) {
					frappe.msgprint(__("Please select {0}", [opts.source_doctype]));
					return;
				}
				opts.source_name = values;
				if (opts.allow_child_item_selection) {
					// args contains filtered child docnames
					opts.args = args;
				}
				d.dialog.hide();
				_map(opts.target);
			},
		});

		return d;
	}

	if (opts.source_name) {
		opts.source_name = [opts.source_name];
		_map();
	}
};

function set_item_attribute_filters_on_fields_in_child_doctype(frm, fields) {
	fields.map(function (field) {
		frm.set_query(field[0], "manufacturing_plan_table", function () {
			return {
				query: "jewellery_erpnext.query.item_attribute_query",
				filters: { item_attribute: field[1] },
			};
		});
	});
}
