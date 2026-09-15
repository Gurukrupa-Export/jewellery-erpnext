frappe.ui.form.on("Material Request", {
	refresh(frm) {
		patch_barcode_scanner_for_serial_no_rows(frm);
		frm.trigger("get_items_from_customer_goods");
		frm.trigger("manufacturing_operation_query");
		if (frm.doc.material_request_type === "Material Transfer") {
			frm.add_custom_button(
				__("Material Transfer (In Transit)"),
				() => frm.events.make_in_transit_stock_entry(frm),
				__("Create")
			);
		}
		// Settle: top up set_from_warehouse with the customer's material (SNC-style)
		// when it is short. Available on a customer-goods Material Transfer that is not
		// cancelled — so it stays on the document after it is submitted, not only while
		// it is a draft.
		if (
			frm.doc.docstatus !== 2 &&
			frm.doc.material_request_type === "Material Transfer" &&
			frm.doc.inventory_type === "Customer Goods"
		) {
			frm.add_custom_button(__("Settle"), function () {
				frappe.call({
					method: "jewellery_erpnext.customer_subcontracting.sub_utils.cg_settle.settle_material_request",
					args: { mr_name: frm.doc.name },
					freeze: true,
					freeze_message: __("Settling..."),
					callback: function (r) {
						if (r.message) {
							frappe.show_alert({
								message: r.message.message,
								indicator: (r.message.settled || []).length ? "green" : "blue",
							});
							frm.reload_doc();
						}
					},
				});
			});
		}
		frm.add_custom_button(
			__("Parent Manufacturing Order"),
			function () {
				erpnext.utils.map_current_doc({
					method: "jewellery_erpnext.jewellery_erpnext.customization.material_request.material_request.get_pmo_data",
					source_doctype: "Parent Manufacturing Order",
					target: frm,
					date_field: "posting_date",
					setters: {
						company: frm.doc.company,
					},
					get_query_filters: {
						docstatus: 1,
					},
					size: "extra-large",
				});
			},
			__("Get Items From")
		);
		// if (!frm.doc.custom_mop_se && frm.doc.docstatus == 1) {
		// 	frm.add_custom_button(__("Transfer To MOP"), () => frm.events.make_stock_entry(frm));
		// }
		// The department route has no button of its own: "Transfer to Department" is a
		// workflow transition, so it appears in the Actions menu beside — and, by its
		// condition on custom_operation_type, instead of — "Transfer to MOP". The old
		// "Update Department" dialog that used to live here is gone; it wrote a
		// stock_entry_type that exists on no site, and would now collide with the real
		// route in exactly the state the real route puts the document in.
		frm.trigger("destination_warehouse_query");
	},
	// Only warehouses that actually sit in the chosen department can receive the
	// material — the server asserts the same thing before it builds the Stock Entry.
	destination_warehouse_query(frm) {
		frm.set_query("custom_destination_warehouse", function () {
			return {
				filters: {
					company: frm.doc.company,
					department: frm.doc.custom_destination_department,
					is_group: 0,
					disabled: 0,
				},
			};
		});
	},
	custom_destination_department(frm) {
		// The warehouse is scoped by the department, so a department change invalidates
		// whatever was picked under the previous one.
		if (frm.doc.custom_destination_warehouse) {
			frm.set_value("custom_destination_warehouse", null);
		}
	},
	custom_operation_type(frm) {
		// Leaving the department route drops a destination that was never acted on: with no
		// Stock Entry behind them those two values describe nothing, and the fields would
		// otherwise stay visible (depends_on keeps any value on show) carrying an abandoned
		// attempt. A completed transfer is the opposite case — its values ARE the record of
		// where the material went, and read_only_depends_on has already locked them — so
		// they are kept whatever the Operation Type is switched to afterwards.
		if (frm.doc.custom_operation_type === "Transfer to Department") return;
		if (frm.doc.custom_department_transfer_se) return;

		// Nulling the department cascades into the handler above, which clears the
		// warehouse; the second call is then a no-op, kept so the intent reads plainly.
		frm.set_value("custom_destination_department", null);
		frm.set_value("custom_destination_warehouse", null);
	},
	// manufacturing_operation_query(frm) {
	// 	frappe.db
	// 		.get_list("Manufacturing Work Order", {
	// 			fields: ["manufacturing_operation"],
	// 			filters: {
	// 				manufacturing_order: frm.doc.manufacturing_order,
	// 				docstatus: 1,
	// 			},
	// 		})
	// 		.then((records) => {
	// 			const mop_list = records.map((item) => item.manufacturing_operation);

	// 			frm.set_query("custom_manufacturing_operation", function () {
	// 				return {
	// 					filters: {
	// 						name: ["in", mop_list],
	// 						department_ir_status: ["not in", "In-Transit"],
	// 						"is_finding":0,
	// 					},
	// 				};
	// 			});
	// 		});
	// },
	manufacturing_operation_query(frm) {
		// The two sources of candidate operations. Resolved into one promise so the
		// department filter below is written once and can never apply to only one of them.
		const mop_names = frm.doc.custom_manufacturing_work_order
			? frappe.db
					.get_list("Manufacturing Operation", {
						fields: ["name"],
						filters: {
							manufacturing_work_order: frm.doc.custom_manufacturing_work_order,
							// Not just "Not Started" -- by the time a request reaches this stage the
							// job's current operation has usually already moved into WIP (or later),
							// and excluding it here made it impossible to pick the one operation that's
							// actually correct. Finished is the only status the server itself rejects
							// (before_update_after_submit's "Cannot select an operation that is already
							// Finished" throw), so mirror that instead of a narrower allow-list.
							status: ["not in", ["Finished"]],
						},
					})
					.then((records) => records.map((item) => item.name))
			: frappe.db
					.get_list("Manufacturing Work Order", {
						fields: ["manufacturing_operation"],
						filters: {
							manufacturing_order: frm.doc.manufacturing_order,
							docstatus: 1,
						},
					})
					.then((records) => records.map((item) => item.manufacturing_operation));

		Promise.all([mop_names, mr_material_department(frm)]).then(([mop_list, department]) => {
			const filters = {
				name: ["in", mop_list],
				department_ir_status: ["not in", "In-Transit"],
			};

			// Only meaningful in the fallback branch above (no custom_manufacturing_work_order
			// yet), where mop_list pools one operation per MWO across the whole manufacturing_order
			// -- some finding, some not -- and this MR's own items decide which kind belongs. Once
			// custom_manufacturing_work_order is set, mop_list is already scoped to that one MWO's
			// own operation(s), so its is_finding status is exactly correct as-is; forcing it to 0
			// would hide a finding MWO's only valid operation from a finding job's own MR.
			if (!frm.doc.custom_manufacturing_work_order) filters.is_finding = 0;

			// Only offer operations doc_events/material_request.validate_mop_department
			// would accept, so the operator does not pick one the server is about to
			// reject. Advisory only — set_query filters the dropdown, it does not stop a
			// typed-in name or an API save, so the server stays the authority.
			//
			// Skipped when the warehouse has no department, rather than filtering on null:
			// that is a setup gap, and the server treats it as a pass.
			if (department) filters.department = department;

			frm.set_query("custom_manufacturing_operation", function () {
				return { filters };
			});
		});
	},
	set_warehouse(frm) {
		// The candidate list is scoped by the material's department, so a change of
		// warehouse invalidates it. refresh() already re-queries on every form load; these
		// two cover a change made without leaving the form.
		frm.trigger("manufacturing_operation_query");
	},
	custom_destination_warehouse(frm) {
		frm.trigger("manufacturing_operation_query");
	},
	// Warn as soon as the operation is picked, rather than letting the operator
	// discover the mismatch when the save throws. Non-blocking on purpose: the field is
	// mandatory in the "Material Transferred" state, so throwing here would make the
	// document unsaveable. The server guard
	// (doc_events/material_request.validate_mop_department) is the hard block.
	custom_manufacturing_operation(frm) {
		const mop = frm.doc.custom_manufacturing_operation;
		const transferred = !!frm.doc.custom_department_transfer_se;
		if (!mop) return;

		Promise.all([
			frappe.db.get_value("Manufacturing Operation", mop, "department"),
			mr_material_department(frm),
		]).then(([mop_res, row_dept]) => {
			const mop_dept = mop_res.message && mop_res.message.department;
			// No exemption, mirroring the server: the MWO's first operation sits in the
			// default department and used to be waved through as a "gathering point",
			// which is exactly how wrong-department operations got in.
			if (mop_dept && row_dept && mop_dept !== row_dept) {
				frappe.show_alert(
					{
						message: transferred
							? __(
									"Operation {0} is in {1}; this material was transferred to {2}. Select an operation in {2}.",
									[mop, mop_dept, row_dept]
							  )
							: __("Material is in {0}; operation {1} is in {2}. Transfer it to {2} first.", [
									row_dept,
									mop,
									mop_dept,
							  ]),
						indicator: "orange",
					},
					10
				);
			}
		});
	},
	// before_workflow_action(frm) {
	// 	if (frm.doc.workflow_state == "Material Transferred") {
	// 		if (!frm.doc.custom_manufacturing_operation) {
	// 			frappe.throw("Please Select Manufacturing Operation");
	// 		}

	// 		frm.events.make_stock_entry(frm);

	// 		// if(!frm.doc.custom_mop_se)
	// 		// 	frappe.throw("Stock Entry Not Created")
	// 	}
	// },
	// make_stock_entry(frm) {
	// 	if (frm.doc.custom_manufacturing_operation) {
	// 		frappe.call({
	// 			method: "jewellery_erpnext.jewellery_erpnext.customization.material_request.material_request.make_mop_stock_entry",
	// 			args: {
	// 				self: frm.doc,
	// 				mop: frm.doc.custom_manufacturing_operation,
	// 			},
	// 			// freeze: true,
	// 			callback: function (r) {
	// 				if (r.message) {
	// 					frappe.msgprint(__("Stock Entry Created"));
	// 					frm.set_value("custom_mop_se", r.message);
	// 				}
	// 				// d.hide();
	// 			},
	// 			error: function (err) {
	// 				console.log;
	// 			},
	// 		});
	// 	}
	// },
	make_in_transit_stock_entry(frm) {
		frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.doc_events.material_request.make_in_transit_stock_entry",
			args: {
				source_name: frm.doc.name,
				to_warehouse: frm.doc.set_warehouse,
				transfer_type: frm.doc.custom_transfer_type,
				pmo: frm.doc.manufacturing_order,
				mnfr: frm.doc.custom_manufacturer,
			},
			callback: function (r) {
				if (r.message) {
					let doc = frappe.model.sync(r.message);
					frappe.set_route("Form", doc[0].doctype, doc[0].name);
				}
			},
		});
	},
	material_request_type(frm) {
		apply_reservation_warehouse(frm);
	},
	custom_manufacturer(frm) {
		apply_reservation_warehouse(frm);
	},
	validate(frm) {
		$.each(frm.doc.items || [], function (i, d) {
			d.custom_insurance_amount = flt(d.custom_insurance_rate) * flt(d.qty);
			// d.serial_no = d.custom_serial_no;
		});
		frm.refresh_field("items");
	},
	get_items_from_customer_goods(frm) {
		if (frm.doc.docstatus === 0) {
			frm.add_custom_button(
				__("Stock Entry"),
				function () {
					erpnext.utils.map_current_doc({
						method: "jewellery_erpnext.jewellery_erpnext.doc_events.material_request.make_stock_in_entry",
						source_doctype: "Stock Entry",
						target: frm,
						date_field: "posting_date",
						setters: {
							stock_entry_type: null,
							purpose: "Material Transfer",
							_customer: frm.doc._customer,
							inventory_type: frm.doc.inventory_type,
						},
						get_query_filters: {
							docstatus: 1,
							purpose: "Material Receipt",
						},
						size: "extra-large",
					});
				},
				__("Get Items From")
			);
		} else {
			frm.remove_custom_button(__("Customer Goods Received"), __("Get Items From"));
		}
	},
});

frappe.ui.form.on("Material Request Item", {
	custom_scan_alternate_item: function (frm, cdt, cdn) {
		let d = locals[cdt][cdn];
		if (d.custom_scan_alternate_item) {
			frappe
				.call({
					method: "erpnext.stock.utils.scan_barcode",
					args: {
						search_value: d.custom_scan_alternate_item,
					},
				})
				.then((r) => {
					frappe.model.set_value(cdt, cdn, "custom_scan_alternate_item", null);
					if (r.message.item_code) {
						frappe.model.set_value(cdt, cdn, "custom_alternative_item", r.message.item_code);
						refresh_field("items");
					} else {
						frappe.msgprint(__("Not able to find Alternative item from Barcode"));
					}
				});
		}
	},
	serial_no: function (frm, cdt, cdn) {
		let child = locals[cdt][cdn];
		const serials = mr_row_serials(child.serial_no);

		if (!serials.length) {
			// Serial cleared: drop the weights with it, so the row cannot keep showing
			// the previous piece's figures.
			let cleared = {};
			FG_BOM_WEIGHT_FIELDS.forEach((f) => (cleared[f] = null));
			frappe.model.set_value(cdt, cdn, cleared);
			return;
		}

		// BOM weights are per-piece, so they only mean anything on a single-serial row.
		if (serials.length !== 1) {
			return;
		}

		// One round trip: the endpoint resolves Serial No -> custom_bom_no -> BOM server
		// side. Deliberately NOT gated on `!child.item_code` the way this used to be --
		// the scanner sets item_code (set_item, step 4) before serial_no (step 5), so
		// that gate meant the weights were never fetched on the scan path at all.
		frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.customization.utils.bom_weights.get_serial_fg_details",
			args: { serial_no: serials[0] },
			callback: function (r) {
				const details = r && r.message;
				if (!details) {
					return;
				}

				let updates = {};
				// Only when blank: on the scan path the scanner has already set this,
				// and re-setting it would re-trigger the item_code handler.
				if (!child.item_code && details.item_code) {
					updates.item_code = details.item_code;
				}

				// No as-built BOM means this is not an FG serial -- stamp nothing.
				if (details.bom_no) {
					updates.bom_no = details.bom_no;
					updates.qty = 1;
					FG_BOM_WEIGHT_FIELDS.forEach((f) => {
						updates[f] = details[f] || 0;
					});
				}

				if (Object.keys(updates).length) {
					frappe.model.set_value(cdt, cdn, updates).then(() => frm.refresh_field("items"));
				}
			},
		});
	},
	item_code(frm, cdt, cdn) {
		frm.trigger("custom_insurance_rate");
		let d = locals[cdt][cdn];
		frappe.db.get_value("Item", d.item_code, ["item_group", "variant_of"], function (r) {
			if (!r) return;

			if (r.item_group == "Metal - V") {
				d.pcs = 1;
				frm.refresh_field("items");
			}

			// custom_variant_of is a fetch_from field, so it is not populated on the row yet
			// at this point. Stamp it from this same round-trip -- the server re-fetches it
			// to the identical value on save -- and derive the warehouse off it.
			frappe.model.set_value(cdt, cdn, "custom_variant_of", r.variant_of || null);
			apply_reservation_warehouse(frm);
		});
		if (d.item_code) {
			var args = {
				company: frm.doc.company,
				item_code: d.item_code,
				warehouse: cstr(d.s_warehouse) || cstr(d.t_warehouse),
				transfer_qty: d.transfer_qty,
				serial_no: d.serial_no,
				batch_no: d.batch_no,
				bom_no: d.bom_no,
				expense_account: d.expense_account,
				cost_center: d.cost_center,
				qty: d.qty,
				voucher_type: frm.doc.doctype,
				voucher_no: d.name,
				allow_zero_valuation: 1,
			};

			return frappe.call({
				method: "jewellery_erpnext.jewellery_erpnext.doc_events.material_request.get_item_details",
				args: {
					args: args,
				},
				callback: function (r) {
					if (r.message) {
						var d = locals[cdt][cdn];
						$.each(r.message, function (k, v) {
							if (v) {
								frappe.model.set_value(cdt, cdn, k, v); // qty and it's subsequent fields weren't triggered
							}
						});
						refresh_field("items");

						// A scan already carries its serial, so the picker has nothing to
						// ask. It used to open once per item code; with one row per
						// serial it would now open on every single scan. The marker
						// lives on the row rather than in frappe.flags because this
						// callback is async and can land after the scanner has cleared
						// its own flags.
						if (!d.__fg_serial_scan) {
							let no_batch_serial_number_value = false;
							if (d.has_serial_no || d.has_batch_no) {
								no_batch_serial_number_value = true;
							}
							frappe.flags.hide_serial_batch_dialog = false;
							frappe.flags.dialog_set = false;

							if (
								no_batch_serial_number_value &&
								!frappe.flags.hide_serial_batch_dialog &&
								!frappe.flags.dialog_set
							) {
								frappe.flags.dialog_set = true;
								frappe.flags.hide_serial_batch_dialog = true;
								erpnext.stock.select_batch_and_serial_no(frm, d);
							} else {
								frappe.flags.dialog_set = false;
							}
						}
					}
				},
			});
		}
	},

	custom_insurance_rate(frm, cdt, cdn) {
		var d = locals[cdt][cdn];
		d.custom_insurance_amount = flt(d.custom_insurance_rate) * flt(d.qty);
		console.log(d.custom_insurance_amount);
		frm.refresh_field("items");
	},
});

// {variant: target_warehouse} from Manufacturer.custom_reservation_table, cached per
// manufacturer on the form so re-deriving on every row edit costs at most one round-trip.
function get_reservation_warehouses(frm, manufacturer) {
	frm.__reservation_warehouses = frm.__reservation_warehouses || {};
	if (frm.__reservation_warehouses[manufacturer]) {
		return Promise.resolve(frm.__reservation_warehouses[manufacturer]);
	}

	return frappe
		.call({
			method: "jewellery_erpnext.jewellery_erpnext.doc_events.material_request.get_reservation_warehouses",
			args: { manufacturer: manufacturer },
		})
		.then((r) => {
			const warehouse_map = (r && r.message) || {};
			frm.__reservation_warehouses[manufacturer] = warehouse_map;
			return warehouse_map;
		});
}

// Derive set_warehouse from the manufacturer's reservation table. Re-runs whenever the item,
// the manufacturer or the request type changes, so switching an M row to a D row re-routes it.
// A value the user typed by hand survives until one of those inputs changes.
function apply_reservation_warehouse(frm) {
	if (frm.doc.material_request_type !== "Manufacture") return;

	const manufacturer = frm.doc.custom_manufacturer || frappe.defaults.get_user_default("manufacturer");
	if (!manufacturer) return;

	const rows = frm.doc.items || [];
	if (!rows.length) return;

	get_reservation_warehouses(frm, manufacturer).then((warehouse_map) => {
		// Every row must resolve to the same warehouse; an unmapped row yields undefined and
		// fails this check, leaving a partially-mappable request alone rather than half-routed.
		const targets = [...new Set(rows.map((row) => warehouse_map[row.custom_variant_of]))];
		if (targets.length !== 1 || !targets[0]) return;
		if (frm.doc.set_warehouse === targets[0]) return;

		// ERPNext's set_warehouse handler cascades this to every row via autofill_warehouse,
		// keeping header and rows consistent so reset_default_field_value cannot clear it.
		frm.set_value("set_warehouse", targets[0]);
	});
}

// Mirrors _current_material_warehouse in doc_events/material_request.py: a completed
// Transfer to Department has moved the material on, so the Request Item warehouse is stale
// from that point and the two would otherwise disagree about where the material sits.
function mr_material_warehouse(frm) {
	if (frm.doc.custom_department_transfer_se) {
		return frm.doc.custom_destination_warehouse;
	}

	const rows = frm.doc.items || [];
	return rows.length ? rows[0].warehouse : null;
}

// Department of the warehouse the material currently sits in -- the value the server
// compares the selected operation against. Cached on the form keyed by the warehouse
// itself, so it self-invalidates the moment the warehouse changes and never goes stale.
function mr_material_department(frm) {
	const warehouse = mr_material_warehouse(frm);
	if (!warehouse) return Promise.resolve(null);

	const cached = frm.__mr_material_department;
	if (cached && cached.warehouse === warehouse) {
		return Promise.resolve(cached.department);
	}

	return frappe.db.get_value("Warehouse", warehouse, "department").then((r) => {
		const department = (r.message && r.message.department) || null;
		frm.__mr_material_department = { warehouse, department };
		return department;
	});
}

// Fields stamped from the scanned serial's own as-built BOM. Mirrors BOM_WEIGHT_FIELDS
// in customization/utils/bom_weights.py -- the server re-stamps the identical values on
// save, so the two lists must not drift.
const FG_BOM_WEIGHT_FIELDS = [
	"custom_bom_gross_weight",
	"custom_bom_metal_weight",
	"custom_bom_finding_weight",
	"custom_bom_diamond_weight",
	"custom_bom_total_diamond_pcs",
	"custom_bom_gemstone_weight",
	"custom_bom_total_gemstone_pcs",
];

function mr_row_serials(value) {
	return String(value || "")
		.split("\n")
		.map((s) => s.trim())
		.filter(Boolean);
}

// Exact, cross-row match. Core's is_duplicate_serial_no inspects only the one candidate
// row and compares with `.includes`, so once every row holds a single serial it stops
// catching repeats altogether -- and it would falsely reject "GK-1" against "GK-12".
function mr_serial_already_scanned(frm, serial_no) {
	return (frm.doc.items || []).some((d) => mr_row_serials(d.serial_no).includes(serial_no));
}

// One scanned FG serial = one row of qty 1.
//
// Stock ERPNext merges every scan of the same item_code into the first matching row and
// increments qty (barcode_scanner.js get_row_to_modify_on_scan). On Material Request that
// is unconditional, because Material Request Item has no `has_item_scanned` field for the
// core guard to check. But each FG piece carries its own as-built BOM and its own
// weights, so a merged row cannot represent them.
//
// Patched rather than solved with a `has_item_scanned` field: a field would only stop the
// merge, while the wrapper below also has to clear frm.has_items, run the duplicate check
// early, and force qty to 1. Same approach as sales_order.js.
function patch_barcode_scanner_for_serial_no_rows(frm) {
	if (frm.__serial_no_barcode_scanner_patched) {
		return;
	}

	let scanner = frm.cscript && frm.cscript.barcode_scanner;
	if (!scanner) {
		return;
	}

	frm.__serial_no_barcode_scanner_patched = true;

	let original_get_row_to_modify_on_scan = scanner.get_row_to_modify_on_scan;
	scanner.get_row_to_modify_on_scan = function (...args) {
		if (!this.__scanning_serial_no) {
			return original_get_row_to_modify_on_scan.apply(this, args);
		}
		// Reuse a genuinely blank row -- a new Material Request opens with one, and
		// returning undefined here would strand it above the first scan -- but never an
		// already-populated one.
		return (this.frm.doc[this.items_table_name] || []).find(
			(d) => !d.item_code && !d[this.serial_no_field]
		);
	};

	// Kept in sync for any other caller that reaches it.
	scanner.is_duplicate_serial_no = function (row, serial_no) {
		if (!serial_no) {
			return false;
		}
		const duplicate = mr_serial_already_scanned(this.frm, serial_no);
		if (duplicate) {
			this.show_alert(__("Serial No {0} is already added", [serial_no]), "orange");
		}
		return duplicate;
	};

	let original_update_table = scanner.update_table;
	scanner.update_table = function (data) {
		this.__scanning_serial_no = !!data.serial_no;
		if (!data.serial_no) {
			// Plain item-barcode scans keep stock merge/increment behaviour.
			return original_update_table.call(this, data);
		}

		// Checked here, before core runs: core checks duplicates only after it has
		// already called add_child, so a late rejection leaves an orphan blank row.
		if (mr_serial_already_scanned(this.frm, data.serial_no)) {
			this.show_alert(__("Serial No {0} is already added", [data.serial_no]), "orange");
			this.clean_up();
			return Promise.reject();
		}

		// frm.has_items is recomputed on every refresh (transaction.js
		// validate_has_items) and is truthy whenever row 1 is populated. Core's set_item
		// then diverts to prepare_item_for_scan -- a modal that never resolves the scan
		// promise -- instead of increment(). Core only clears the flag inside its own
		// "new row" branch, which reusing a blank row skips, so clear it here.
		this.frm.has_items = false;

		return original_update_table.call(this, data).then((row) => {
			if (!row) {
				return row;
			}
			// Marks the row for the whole scan, including async callbacks that resolve
			// after the scanner's own flags are cleared. Not persisted.
			locals[row.doctype][row.name].__fg_serial_scan = true;
			return frappe.model.set_value(row.doctype, row.name, "qty", 1).then(() => row);
		});
	};
}

erpnext.stock.select_batch_and_serial_no = (frm, item) => {
	let path = "assets/erpnext/js/utils/serial_no_batch_selector.js";

	frappe.db.get_value("Item", item.item_code, ["has_batch_no", "has_serial_no"]).then((r) => {
		if (r.message && (r.message.has_batch_no || r.message.has_serial_no)) {
			item.has_serial_no = r.message.has_serial_no;
			item.has_batch_no = r.message.has_batch_no;
			item.type_of_transaction = item.s_warehouse ? "Outward" : "Inward";

			new erpnext.SerialBatchPackageSelector(frm, item, (r) => {
				var sr_list = [];
				if (r) {
					if (r.entries) {
						r.entries.forEach((element) => {
							if (item.has_batch_no) {
								frappe.model.set_value(item.doctype, item.name, {
									batch_no: element.batch_no,
									qty:
										Math.abs(r.total_qty) /
										flt(
											item.conversion_factor || 1,
											precision("conversion_factor", item)
										),
								});
							} else if (item.has_serial_no) {
								sr_list.push(element.serial_no);
							}
						});
						if (sr_list) {
							var serial_no = sr_list.join(",");
							frappe.model.set_value(item.doctype, item.name, "serial_no", serial_no);
						}
					}
				}
			});
		}
	});
};
