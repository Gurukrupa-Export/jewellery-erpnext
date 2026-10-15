// Copyright (c) 2024, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Metal Conversions", {
	refresh(frm) {
		if (frm.doc.multiple_metal_converter == 0) {
			if (frm.doc.source_item) {
				set_batch_filter(frm, "batch");
			}
		}
		if (frm.doc.multiple_metal_converter == 1) {
			set_child_table_batch_filter(frm, "mc_source_table");
		}
		frm.fields_dict["source_item"].get_query = function (frm) {
			return {
				query: "jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.filters.item_query_filters",
			};
		};
		// Remarks ships no options in the DocType JSON, so a saved doc has nothing to
		// render its stored value against until this runs.
		set_remark_options(frm);
		toggle_chain_fields(frm);
	},
	setup(frm) {
		// Set Metal Tab Filter
		set_wh_filter(frm, "source_warehouse");
		set_department_filter(frm, "department");
		if (frm.doc.multiple_metal_converter == 0) {
			set_Metal_filter(frm, "source_item");
			set_Metal_filter(frm, "target_item");
			set_alloy_filter(frm, "source_alloy");
			set_alloy_filter(frm, "target_alloy");
		} else {
			set_source_metal_table_filter(frm, "item_code", "mc_source_table");
			set_Metal_filter(frm, "m_target_item");
			set_alloy_filter(frm, "alloy");
		}
	},
	employee(frm) {
		get_detail_tab_value(frm);
	},
	source_warehouse(frm) {
		frm.set_value("target_warehouse", frm.doc.source_warehouse);
		frm.refresh_field("target_warehouse");
	},
	percentage(frm) {
		set_remark_options(frm);
	},
	multiple_metal_converter(frm) {
		// For Clearing All Field's
		frappe.call({
			method: "clear_fields",
			doc: frm.doc,
			args: {
				docname: frm.doc.name,
			},
			callback(r) {
				frm.save();
			},
		});
	},
	source_item(frm) {
		clear_metal_field(frm);
		frm.set_value("source_qty", null);
		set_batch_filter(frm, "batch");
	},
	source_qty(frm) {
		// Clear All Fields
		clear_metal_field(frm);
	},
	is_melting_loss(frm) {
		if (frm.doc.is_melting_loss) {
			// Loss-recording mode: drop every conversion-only field.
			clear_metal_field(frm);
		} else {
			frm.set_value("loss_qty", null);
		}
	},
	batch(frm) {
		// customer and inventory_type are deliberately NOT written here any more: both
		// are derived server-side from the FIFO allocation (build_conversion_lanes),
		// which can span several ownerships at once. Setting them from a single batch
		// would contradict the Conversion Lanes table.
		frm.set_value("batch_available_qty", null);
		frm.set_value("supplier", null);
		frappe.call({
			method: "get_batch_detail",
			doc: frm.doc,
			args: {
				docname: frm.doc.name,
			},
			callback: (r) => {
				if (!r.message) return;
				frm.set_value("batch_available_qty", r.message[0]);
				frm.set_value("supplier", r.message[1]);

				frm.refresh_field("batch_available_qty");
				frm.refresh_field("supplier");
			},
		});
	},
	target_item(frm) {
		// A chain conversion's Target Qty is what came back from the employee or supplier,
		// typed in at Receive -- not a purity calculation. Its batches were drawn on the
		// server and moved by Issue, so they are never cleared here.
		if (is_chain(frm)) {
			set_chain_loss(frm);
			return;
		}
		// Calculate Metal
		calculate_metal(frm);
	},
	calculate(frm) {
		calculate_Multiple_conversion(frm);
	},
	is_chain_manufacturing(frm) {
		if (is_chain(frm)) {
			// No alloy and no target until Receive.
			clear_metal_field(frm);
		} else {
			frm.set_value({ to_employee: null, chain_supplier: null, is_chain_subcontracting: 0 });
		}
		toggle_chain_fields(frm);
	},
	is_chain_subcontracting(frm) {
		// One party converts the metal: the Supplier when ticked, else the To Employee.
		if (frm.doc.is_chain_subcontracting) {
			frm.set_value("to_employee", null);
		} else {
			frm.set_value("chain_supplier", null);
		}
		toggle_chain_fields(frm);
	},
	to_employee(frm) {
		if (!frm.doc.to_employee) {
			frm.set_value({ to_department: null, to_warehouse: null });
			return;
		}
		refresh_chain_details(frm);
	},
	chain_supplier(frm) {
		if (!frm.doc.chain_supplier) {
			frm.set_value("chain_supplier_warehouse", null);
			return;
		}
		refresh_chain_details(frm);
	},
	target_qty(frm) {
		set_chain_loss(frm);
	},
	before_workflow_action(frm) {
		if (frm.selected_workflow_action === "Cancel Issue") {
			return confirm_cancel_issue(frm);
		}
		if (frm.selected_workflow_action !== "Receive") return;
		const missing = [];
		if (!frm.doc.target_item) missing.push(__("Target Item"));
		if (!flt(frm.doc.target_qty)) missing.push(__("Target Qty"));
		let message = null;
		if (missing.length) {
			message = __("Enter {0} before Receive.", [missing.join(", ")]);
		} else if (flt(frm.doc.target_qty) > flt(frm.doc.source_qty)) {
			message = __("Target Qty cannot be greater than Source Qty.");
		}
		if (message) {
			// workflow.js froze the page and only unfreezes after the action runs.
			frappe.dom.unfreeze();
			frappe.throw(message);
		}
	},
});
frappe.ui.form.on("MC Source Table", {
	item_code: function (frm, cdt, cdn) {
		var child_doc = locals[cdt][cdn];
		frappe.model.set_value(child_doc.doctype, child_doc.name, "qty", null);
		frappe.model.set_value(child_doc.doctype, child_doc.name, "batch", null);
		frappe.model.set_value(child_doc.doctype, child_doc.name, "batch_available_qty", null);
		frappe.model.set_value(child_doc.doctype, child_doc.name, "inventory_type", null);
		frappe.model.set_value(child_doc.doctype, child_doc.name, "customer", null);
		frappe.model.set_value(child_doc.doctype, child_doc.name, "supplier", null);
		frappe.model.set_value(child_doc.doctype, child_doc.name, "total", null);
	},
	qty: function (frm, cdt, cdn) {
		// calculateSum(frm);
		var child = locals[cdt][cdn];
		if (child.qty > 0) {
			frappe.call({
				method: "get_mc_table_purity",
				doc: frm.doc,
				args: {
					item_code: child.item_code,
					qty: child.qty,
				},
				callback: (r) => {
					if (r.message) {
						var child_doc = locals[cdt][cdn];
						frappe.model.set_value(child_doc.doctype, child_doc.name, "total", r.message[0]);
					}
				},
			});
		}
	},
	batch: function (frm, cdt, cdn) {
		set_batch_value(frm, cdt, cdn);
	},
});
function set_remark_options(frm) {
	// Remarks is a Select whose option TEXT carries the document's Percentage, so the
	// list is built per document instead of living in the DocType JSON (where it would
	// be shared by every document). The server renders it -- see
	// metal_conversions.py::render_remark_options -- and re-renders the stored value on
	// validate, so what the dropdown shows and what we store can never drift.
	if (!frm.fields_dict["remarks"]) return;

	const previous = frm.fields_dict["remarks"].df.options;
	const picked = Array.isArray(previous) ? previous.indexOf(frm.doc.remarks) : -1;

	frappe.call({
		method: "get_remark_options",
		doc: frm.doc,
		callback: (r) => {
			const options = [""].concat(r.message || []);
			frm.set_df_property("remarks", "options", options);

			// Both lists are [""] + templates in the same order, so the index survives a
			// Percentage change -- re-point a picked remark at the re-rendered sentence.
			if (picked > 0 && options[picked] && options[picked] !== frm.doc.remarks) {
				frm.set_value("remarks", options[picked]);
			}
			frm.refresh_field("remarks");
		},
	});
}

function set_wh_filter(frm, field_name) {
	frm.set_query(field_name, function () {
		return {
			filters: {
				department: frm.doc.department,
			},
		};
	});
}
function set_department_filter(frm, field_name) {
	frm.set_query(field_name, function () {
		return {
			filters: {
				company: frm.doc.company,
			},
		};
	});
}
function set_Metal_filter(frm, field_name) {
	frm.set_query(field_name, function () {
		return {
			filters: {
				variant_of: ["in", ["M", "F"]],
			},
		};
	});
}
function set_source_metal_table_filter(frm, field, table_name) {
	frm.set_query(field, table_name, () => {
		return {
			filters: {
				variant_of: ["in", ["M", "F"]],
			},
		};
	});
}
function set_batch_filter(frm, field_name) {
	frm.fields_dict[field_name].get_query = function (doc) {
		return {
			query: "jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions.get_filtered_batches",
			filters: {
				item_code: frm.doc.source_item,
				warehouse: frm.doc.source_warehouse,
				company: frm.doc.company,
			},
		};
	};
}
function set_child_table_batch_filter(frm, child_table_name) {
	frm.fields_dict[child_table_name].grid.get_field("batch").get_query = function (doc, cdt, cdn) {
		var child = locals[cdt][cdn];
		console.log(child.item_code);
		return {
			query: "jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions.get_filtered_batches",
			filters: {
				item_code: child.item_code,
				warehouse: frm.doc.source_warehouse,
				company: frm.doc.company,
			},
		};
	};
}
function set_alloy_filter(frm, field_name) {
	frm.set_query(field_name, function () {
		return {
			filters: {
				item_group: "Alloy",
			},
		};
	});
}
function get_detail_tab_value(frm) {
	frappe.call({
		method: "get_detail_tab_value",
		doc: frm.doc,
		args: {
			docname: frm.doc.name,
		},
		callback: function (r) {
			frm.refresh_field("department");
			frm.refresh_field("manufacturer");
			frm.refresh_field("branch");
			frm.refresh_field("source_warehouse");
			frm.refresh_field("target_warehouse");
			frm.refresh_fields();
			toggle_chain_fields(frm);
		},
	});
}

// ---- Chain Manufacturing (doc_events/chain_subcontracting.py) ----

// Chain Manufacturing in either mode: to the To Employee, or to a Supplier.
function is_chain(frm) {
	return frm.doc.is_chain_manufacturing === "Yes";
}

// The To Employee's and the Supplier's warehouses come from their masters.
function refresh_chain_details(frm) {
	frappe.call({
		method: "refresh_chain_details",
		doc: frm.doc,
		callback() {
			frm.refresh_fields();
			toggle_chain_fields(frm);
		},
	});
}

function set_chain_loss(frm) {
	if (!is_chain(frm)) return;
	const loss = frm.doc.target_qty ? flt(frm.doc.source_qty) - flt(frm.doc.target_qty) : 0;
	frm.set_value("chain_process_loss_qty", loss);
}

function toggle_chain_fields(frm) {
	const chain = is_chain(frm);
	const issued = !!frm.doc.chain_issue_stock_entry;

	// The single/multiple, melting-loss and alloy modes do not apply to a chain conversion.
	frm.toggle_display(["multiple_metal_converter", "is_melting_loss"], !chain);
	// The allocated batch is shown, never picked: the server fills it from the FIFO draw.
	frm.toggle_display("batch", chain && !!frm.doc.batch);
	frm.set_df_property("batch", "read_only", chain ? 1 : 0);
	frm.set_df_property("source_batch_details", "read_only", chain ? 1 : 0);

	// Issue has moved the metal: what it moved can no longer change.
	[
		"employee",
		"is_chain_manufacturing",
		"is_chain_subcontracting",
		"to_employee",
		"chain_supplier",
		"source_item",
		"source_qty",
	].forEach((field) => frm.set_df_property(field, "read_only", issued ? 1 : 0));
}

// Cancel Issue undoes the Issue entry and deletes the draft PO, so it asks first. workflow.js
// froze the page before this hook; the dialog needs it unfrozen, and a refusal stops the action.
function confirm_cancel_issue(frm) {
	frappe.dom.unfreeze();
	const message = frm.doc.chain_purchase_order
		? __(
				"Cancel Issue cancels {0}, deletes draft Purchase Order {1} and returns this conversion to Draft. Continue?",
				[frm.doc.chain_issue_stock_entry, frm.doc.chain_purchase_order]
		  )
		: __("Cancel Issue cancels {0} and returns this conversion to Draft. Continue?", [
				frm.doc.chain_issue_stock_entry,
		  ]);
	return new Promise((resolve, reject) =>
		frappe.confirm(
			message,
			() => {
				frappe.dom.freeze();
				resolve();
			},
			() => reject()
		)
	);
}
function calculate_metal(frm) {
	if (frm.doc.target_item) {
		frappe.call({
			method: "calculate_metal_conversion",
			doc: frm.doc,
			args: {
				docname: frm.doc.name,
			},
			callback: function (r) {
				if (r.message) {
					frm.set_value("target_qty", r.message[0]);
					frm.refresh_field("target_qty");

					if (r.message[1] < 0) {
						clear_alloy(frm);
						frm.set_value("target_alloy_check", 1);
						frm.set_value("target_alloy_qty", Math.abs(r.message[1]));
						frm.refresh_field("target_alloy_check");
						frm.refresh_field("target_alloy_qty");
						// frm.save();
					} else if (r.message[1] > 0) {
						clear_alloy(frm);
						frm.set_value("source_alloy_check", 1);
						frm.set_value("source_alloy_qty", r.message[1]);

						frm.refresh_field("source_alloy_check");
						frm.refresh_field("source_alloy_qty");
						// frm.save();
					} else {
						clear_alloy(frm);
						frappe.show_alert(
							{
								message: __("Alloy Selection Invisible Due to Calculation is <b>0</b>"),
								indicator: "green",
							},
							5
						);
					}
				}
			},
		});
	}
}
function clear_alloy(frm) {
	frm.set_value("source_alloy_check", "0");
	frm.set_value("source_alloy", null);
	frm.set_value("source_alloy_qty", null);
	frm.set_value("target_alloy_check", "0");
	frm.set_value("target_alloy", null);
	frm.set_value("target_alloy_qty", null);

	frm.refresh_field("source_alloy_check");
	frm.refresh_field("source_alloy");
	frm.refresh_field("source_alloy_qty");
	frm.refresh_field("target_alloy_check");
	frm.refresh_field("target_alloy");
	frm.refresh_field("target_alloy_qty");
}
function calculate_Multiple_conversion(frm) {
	frappe.call({
		method: "calculate_Multiple_conversion",
		doc: frm.doc,
		args: {
			docname: frm.doc.name,
		},
		callback: (r) => {
			console.log(r.message);
			if (r.message) {
				frm.set_value("alloy_qty", 0);
				frm.set_value("m_target_qty", r.message[0]);
				frm.refresh_field("m_target_qty");

				if (r.message[1] < 0) {
					// if alloy_qty return minus then consider is target side stock entry
					frm.set_value("alloy_check", 1);
					frm.refresh_field("alloy_check");
					frm.set_value("alloy_qty", Math.abs(r.message[1]));
					frm.refresh_field("alloy_qty");
					// frm.save();
				}
				if (r.message[1] > 0) {
					frm.set_value("alloy_check", 0);
					frm.refresh_field("alloy_check");
					frm.set_value("alloy_qty", r.message[1]);
					frm.refresh_field("alloy_qty");
					// frm.save();
				}
			}
		},
	});
}
function validate_alloy(frm) {
	if (frm.doc.source_alloy_check == 1 && frm.doc.source_alloy == null) {
		frappe.throw(__("Source Alloy is Missing"));
	}
	if (frm.doc.target_alloy_check == 1 && frm.doc.target_alloy == null) {
		frappe.throw(__("Target Alloy is Missing"));
	}
}
function clear_metal_field(frm) {
	frm.set_value("target_item", null);
	frm.set_value("target_qty", null);
	frm.set_value("source_alloy", null);
	frm.set_value("target_alloy", null);
	frm.set_value("source_alloy_qty", null);
	frm.set_value("target_alloy_qty", null);
	frm.set_value("source_alloy_check", "0");
	frm.set_value("target_alloy_check", "0");
	frm.set_value("batch", null);
	// frm.save();
}

// To Set Batch Bailance and Respective Details into child table
function set_batch_value(frm, cdt, cdn) {
	var child_doc = locals[cdt][cdn];
	frappe.call({
		method: "get_child_batch_detail",
		doc: frm.doc,
		args: {
			// docname: frm.doc.name,
			table_item: child_doc.item_code,
			talble_source_warehouse: frm.doc.source_warehouse,
			table_batch: child_doc.batch,
		},
		callback: (r) => {
			if (r.message) {
				var child_doc = locals[cdt][cdn];

				console.log(r.message);
				frappe.model.set_value(
					child_doc.doctype,
					child_doc.name,
					"batch_available_qty",
					r.message[0]
				);
				frappe.model.set_value(child_doc.doctype, child_doc.name, "supplier", r.message[1]);
				frappe.model.set_value(child_doc.doctype, child_doc.name, "customer", r.message[2]);
				if (!r.message[3]) {
					frappe.model.set_value(
						child_doc.doctype,
						child_doc.name,
						"inventory_type",
						"Regular Stock"
					);
				} else {
					frappe.model.set_value(child_doc.doctype, child_doc.name, "inventory_type", r.message[3]);
				}
			}
		},
	});
}
