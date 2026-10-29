// Copyright (c) 2023, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Manufacturing Plan", {
	onload(frm) {
		frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan.get_allowed_manufacture_orders",
			callback: function (r) {
				frm.allowed_manufacture_orders = r.message || [];
				apply_manufacture_order_options(frm);
			},
		});
		// A draft, Duplicate or route option can open a plan already in Without-SO mode.
		if (
			frm.doc.docstatus === 0 &&
			frm.doc.select_manufacture_order === "Finding Manufacturing - Without SO"
		) {
			set_user_department(frm);
		}
	},
	refresh(frm) {
		apply_manufacture_order_options(frm);
		if (frm.doc.docstatus == 1) frm.trigger("show_progress");
		if (frm.doc.docstatus == 1) {
			frm.add_custom_button(__("Create CAD MWO"), function () {
				frappe.call({
					method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan.get_cad_eligible_items",
					args: {
						manufacturing_plan: frm.doc.name,
					},
					callback: function (r) {
						let item_codes = r.message || [];
						if (!item_codes.length) {
							frappe.msgprint(
								__(
									"All items in this Manufacturing Plan already have a CAD Manufacturing Work Order."
								)
							);
							return;
						}
						show_cad_item_select_dialog(frm, item_codes);
					},
				});
			});
		}
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
		set_finding_detail_filters(frm);
	},
	select_manufacture_order(frm) {
		// Department belongs to the Without-SO mode only, and defaults to the user's Employee department.
		if (frm.doc.select_manufacture_order !== "Finding Manufacturing - Without SO") {
			if (frm.doc.department) frm.set_value("department", null);
			return;
		}
		set_user_department(frm);
	},
	get_sales_order(frm) {
		map_current_doc({
			method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan.get_details_to_append",
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
			method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan.get_details_to_append",
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
			method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan.get_details_to_append",
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

// Offer only the modes the user's roles allow (Manufacturing Setting > Manufacture Order Roles),
// keeping the plan's current mode so a draft raised by someone else still shows it.
function apply_manufacture_order_options(frm) {
	if (!frm.allowed_manufacture_orders) return;
	const options = ["", ...frm.allowed_manufacture_orders];
	const current = frm.doc.select_manufacture_order;
	if (current && !options.includes(current)) options.push(current);
	frm.set_df_property("select_manufacture_order", "options", options.join("\n"));
	// Set on every refresh: the form clears its intro message each time it renders.
	if (frm.is_new() && !frm.allowed_manufacture_orders.length) {
		frm.set_intro(
			__(
				"No Select Manufacture Order option is open to your roles. Ask a System Manager to set its roles in Manufacturing Setting."
			),
			"orange"
		);
	}
}

function set_user_department(frm) {
	// A user with an Employee department gets it filled in and locked; a user without one picks
	// it. A saved department is kept either way.
	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan.get_user_department",
		callback: function (r) {
			if (frm.doc.select_manufacture_order !== "Finding Manufacturing - Without SO") return;
			frm.set_df_property("department", "read_only", r.message ? 1 : 0);
			if (r.message && !frm.doc.department) frm.set_value("department", r.message);
		},
	});
}

function show_cad_item_select_dialog(frm, item_codes) {
	let rows_html = item_codes
		.map(
			(code) => `
			<tr>
				<td style="width: 40px; text-align: center;">
					<input type="checkbox" class="cad-mwo-item-check" data-item-code="${frappe.utils.escape_html(code)}">
				</td>
				<td>${frappe.utils.escape_html(code)}</td>
			</tr>`
		)
		.join("");

	let dialog = new frappe.ui.Dialog({
		title: __("Select Item Code"),
		fields: [
			{
				fieldname: "items_html",
				fieldtype: "HTML",
				options: `
					<div style="max-height: 350px; overflow-y: auto;">
						<table class="table table-bordered">
							<thead>
								<tr>
									<th style="width: 40px; text-align: center;">
										<input type="checkbox" class="cad-mwo-select-all">
									</th>
									<th>${__("Item Code")}</th>
								</tr>
							</thead>
							<tbody>${rows_html}</tbody>
						</table>
					</div>
				`,
			},
		],
		primary_action_label: __("Next"),
		primary_action: function () {
			let selected_items = dialog.$wrapper
				.find(".cad-mwo-item-check:checked")
				.map(function () {
					return $(this).attr("data-item-code");
				})
				.get();

			if (!selected_items.length) {
				frappe.msgprint(__("Please select at least one Item Code"));
				return;
			}
			dialog.hide();
			show_cad_reason_dialog(frm, selected_items);
		},
	});

	dialog.$wrapper.find(".cad-mwo-select-all").on("change", function () {
		dialog.$wrapper.find(".cad-mwo-item-check").prop("checked", $(this).is(":checked"));
	});

	dialog.show();
}

function show_cad_reason_dialog(frm, item_codes) {
	frappe.prompt(
		[
			{
				fieldname: "reason",
				label: "Reason",
				fieldtype: "Select",
				reqd: 1,
				options: [
					"Cpx rpt",
					"Mould broken & extra need for bulk order",
					"Prong thickness & height for wax setting",
					"Mumbai CAD, if CAD image show in (ppc wax cad) then we have to transfer for rubber mould work",
				].join("\n"),
			},
		],
		function (reason_data) {
			frappe.call({
				method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan.create_cad_mwo_bulk",
				args: {
					manufacturing_plan: frm.doc.name,
					item_codes: item_codes,
					reason: reason_data.reason,
				},
				callback: function (r) {
					if (!r.exc) {
						frm.reload_doc();
					}
				},
			});
		},
		__("Select Reason"),
		__("Submit")
	);
}

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
							__("You have already selected items from {0} {1}", [opts.source_doctype, src])
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
			method: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan.map_docs",
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

// Finding Details columns that are attributes of the F template. Each one's dropdown only
// offers values that exist on an F item together with the row's other picks.
const FINDING_ATTRIBUTE_FIELDS = [
	"finding_category",
	"finding_type",
	"metal_type",
	"metal_touch",
	"metal_purity",
	"metal_colour",
	"finding_size",
];

function set_finding_detail_filters(frm) {
	FINDING_ATTRIBUTE_FIELDS.forEach((fieldname) => {
		frm.set_query(fieldname, "finding_details", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			const selected = {};
			FINDING_ATTRIBUTE_FIELDS.forEach((other) => {
				if (other !== fieldname && row[other]) selected[other] = row[other];
			});
			return {
				query: "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan.finding_attribute_query",
				filters: { fieldname, selected },
			};
		});
	});
	// Not an F attribute, so it is not narrowed by the row.
	frm.set_query("setting_type", "finding_details", () => ({
		query: "jewellery_erpnext.query.item_attribute_query",
		filters: { item_attribute: "Setting Type" },
	}));
}

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
