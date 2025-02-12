// Copyright (c) 2023, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Refining", {
	setup(frm) {
		filter_fields_based_on_company(frm, "refining_department");
		filter_fields_based_on_company(frm, "department");
		filter_items(frm, "recovered_diamond", "D");
		filter_items(frm, "recovered_gemstone", "G");
		filter_items(frm, "refined_gold", "M");

		filter_recovery_source(frm, "department", "refining_department_detail");
		filter_recovery_source(frm, "operation", "refining_operation_detail");
	},
	refresh(frm) {
		set_html(frm);
		transfer_dust_btn(frm);

		// for purity link field
		frm.set_query("metal_purity", function (doc) {
			return {
				query: "jewellery_erpnext.query.item_attribute_query",
				filters: { item_attribute: "Metal Purity" },
			};
		});
	},
	validate(frm) {
		if (!frm.doc.multiple_operation) {
			frm.clear_table("refining_operation_details");
			frm.refresh_field("refining_operation_details");
		} else if (!frm.doc.multiple_department) {
			frm.clear_table("refining_department_detail");
			frm.refresh_field("refining_department_detail");
		} else {
			frm.set_value("department", null);
			frm.set_value("employee", null);
			frm.set_value("operation", null);
		}

		check_metal_weight_pure_wheight(frm);
	},
	previous_refining(frm) {
		frappe.db.get_doc("Refining", frm.doc.previous_refining).then((doc) => {
			if (doc.docstatus === 1 && doc.dust_received === 1) {
				frm.set_value("refining_department", doc.refining_department);
				frm.set_value("source_warehouse", doc.refining_warehouse);
				for (let row of doc.refined_gold) {
					let child1 = frm.add_child("refined_gold", {
						dust_item: row.item_code,
						dust_weight: row.pure_weight,
					});
				}
				frm.refresh_field("refined_gold");
			} else {
				frappe.throw(
					`Previous Refining Is not Complete Yet : ${frm.doc.previous_refining}`
				);
			}
		});
	},
	date_from(frm) {
		var today = frappe.datetime.now_date();
		var entered_date = frm.doc.date_from;
		if (entered_date > today) {
			frappe.msgprint(__("Future dates are not allowed!"));
			frm.set_value("date_from", today);
		} else if (entered_date > frm.doc.date_to) {
			frappe.msgprint(__("'Date From' cannot be after 'Date To'"));
			frm.set_value("date_from", frm.doc.date_to);
		}
	},
	date_to(frm) {
		var today = frappe.datetime.now_date();
		var entered_date = frm.doc.date_to;
		if (entered_date > today) {
			frappe.msgprint(__("Future dates are not allowed!"));
			frm.set_value("date_to", today);
		} else if (entered_date < frm.doc.date_from) {
			frappe.msgprint(__("'Date To' cannot be before 'Date From'"));
			frm.set_value("date_to", frm.doc.date_from);
		}
	},
	metal_purity(frm) {
		if (frm.doc.metal_purity > 100 || frm.doc.metal_purity < 0) {
			frappe.msgprint(__("metal_purity must be between 0 to 100"));
			frm.set_value("metal_purity", 0);
		}
		let fine_weight = (flt(frm.doc.refining_gold_weight) * flt(frm.doc.metal_purity)) / 100;
		frm.set_value("fine_weight", fine_weight);
	},
	refining_gold_weight(frm) {
		frm.trigger("metal_purity");
	},
	refining_type(frm) {
		set_series(frm);
		set_html(frm);
		transfer_dust_btn(frm);
	},
	refining_department(frm) {
		frappe.db
			.get_value("Warehouse", { department: frm.doc.refining_department }, "name")
			.then((r) => {
				console.log(r.message.name); // Open
				frm.set_value("refining_warehouse", r.message.name);
			});
	},
	scan_barcode(frm) {
		get_item_by_serial_no(frm);
	},
	get_parent_production_order(frm) {
		if (!frm.doc.refining_department) {
			frappe.throw(__("Please select refining department first"));
		}
		var query_filters = {
			company: frm.doc.company,
		};
		if (frm.doc.refining_type == "Parent Manufacturing Order") {
			query_filters["department_ir_status"] = ["not in", ["In-Transit", "Revert"]];
			query_filters["status"] = ["in", ["Not Started"]];
			// query_filters["employee"] = ["is", "not set"]
			// query_filters["subcontractor"] = ["is", "not set"]
		} else {
			query_filters["department_ir_status"] = "In-Transit";
			query_filters["department"] = frm.doc.refining_department;
		}
		erpnext.utils.map_current_doc({
			method: "jewellery_erpnext.jewellery_erpnext.doctype.refining.refining.get_manufacturing_operations",
			source_doctype: "Manufacturing Operation",
			target: frm,
			setters: {
				manufacturing_work_order: undefined,
				manufacturing_order: undefined,
				department: frm.doc.refining_department || undefined,
				company: frm.doc.company || undefined,
			},
			get_query_filters: query_filters,
			size: "extra-large",
		});
	},
	department(frm) {
		if (frm.doc.employee) {
			get_source_warehouse(frm, "employee", frm.doc.employee);
		} else if (frm.doc.operation) {
			get_source_warehouse(frm, "custom_manufacturing_operation", frm.doc.operation);
		} else {
			get_source_warehouse(frm, "department", frm.doc.department);
		}
	},
	operation(frm) {
		if (frm.doc.employee) {
			get_source_warehouse(frm, "employee", frm.doc.employee);
		} else {
			get_source_warehouse(frm, "custom_manufacturing_operation", frm.doc.operation);
		}
	},
	employee(frm) {
		get_source_warehouse(frm, "employee", frm.doc.employee);
	},
});

frappe.ui.form.on("Refined Gold", {
	refining_gold_weight(frm, cdt, cdn) {
		let d = locals[cdt][cdn];
		if (d.refining_gold_weight && d.metal_purity) {
			d.pure_weight = 0;
			frm.refresh_field("refined_gold");
			d.pure_weight = d.refining_gold_weight * (d.metal_purity / 100);
			frm.refresh_field("refined_gold");
		}
		if (d.refining_gold_weight < d.pure_weight) {
			frappe.throw(__("Refining Gold Weight cannot be greater than Pure Weight"));
		}
	},
	metal_purity(frm, cdt, cdn) {
		let d = locals[cdt][cdn];
		if (d.refining_gold_weight && d.metal_purity) {
			d.pure_weight = 0;
			frm.refresh_field("refined_gold");
			d.pure_weight = d.refining_gold_weight * (d.metal_purity / 100);
			frm.refresh_field("refined_gold");
		}
		if (d.refining_gold_weight < d.pure_weight) {
			frappe.throw(__("Refining Gold Weight cannot be greater than Pure Weight"));
		}
	},
	pure_weight(frm, cdt, cdn) {
		let d = locals[cdt][cdn];
		if (d.refining_gold_weight < d.pure_weight) {
			frappe.throw(__("Refining Gold Weight cannot be greater than Pure Weight"));
		}
	},
});

function check_metal_weight_pure_wheight(frm) {
	if (frm.doc.refined_gold) {
		for (let row of frm.doc.refined_gold) {
			if (row.refining_gold_weight < row.pure_weight) {
				frappe.throw(__("Refining Gold Weight cannot be greater than Pure Weight"));
			}
		}
	}
}

function set_series(frm) {
	if (frm.doc.refining_type === "Parent Manufacturing Order") {
		frm.set_value("naming_series", "RFN-PMO-.YY.-.#####");
		frm.refresh_field("naming_series");
	} else if (frm.doc.refining_type === "Serial Number") {
		frm.set_value("naming_series", "RFN-SRN-.YY.-.#####");
		frm.refresh_field("naming_series");
	} else if (frm.doc.refining_type === "Recovery Material") {
		frm.set_value("naming_series", "RFN-RCM-.YY.-.#####");
		frm.refresh_field("naming_series");
	} else if (frm.doc.refining_type === "Re-Refining Material") {
		frm.set_value("naming_series", "RFN-RER-.YY.-.#####");
		frm.refresh_field("naming_series");
	}
}

function set_html(frm) {
	frm.get_field("raw_material_table").$wrapper.html("");
	if (!frm.doc.__islocal) {
		//ToDo: add function for stock entry detail for normal manufacturing operations
	} else {
		frm.get_field("raw_material_table").$wrapper.html("");
	}
	if (frm.doc.refining_type === "Parent Manufacturing Order") {
		frappe.call({
			method: "get_linked_stock_entries",
			doc: frm.doc,
			args: {
				docname: frm.doc.name,
			},
			callback: function (r) {
				frm.get_field("raw_material_table").$wrapper.html(r.message);
				frm.doc.set_df_property("raw_material_table", "hidden", 0);
			},
		});
	}
}

function get_item_by_serial_no(frm) {
	if (frm.doc.scan_barcode) {
		if (!frm.doc.refining_department) {
			frappe.throw(__("Please select refining department first"));
		}
		var query_filters = {
			company: frm.doc.company,
			name: frm.doc.scan_barcode,
			// "warehouse": frm.doc.refining_warehouse,
			status: "Active",
		};

		frappe.db
			.get_value("Serial No", query_filters, ["name", "item_code", "warehouse"])
			.then((r) => {
				let values = r.message;
				// console.log(values)
				if (values.name) {
					if (values.warehouse != frm.doc.refining_warehouse) {
						frappe.throw(
							`Serial No : <strong>${values.name}</strong> is not In Refining Warehouse`
						);
					}
					add_child_in_serial_number(frm, values);
					frm.refresh_field("refining_serial_no_detail");
				} else {
					frappe.throw(__("Invalid Serial No"));
				}
				frm.set_value("scan_barcode", "");
			});
	}
}

async function add_child_in_serial_number(frm, values) {
	try {
		const r = await frappe.db.get_value("BOM", { item: values.item_code }, [
			"name",
			"metal_and_finding_weight",
			"gross_weight",
			"custom_net_pure_weight",
		]);

		let pure_weight = 0.001;
		if (r.message.custom_net_pure_weight) {
			pure_weight = r.message.custom_net_pure_weight;
		}
		frm.add_child("refining_serial_no_detail", {
			serial_number: values.name,
			item_code: values.item_code,
			pure_weight: flt(pure_weight),
			gross_weight: flt(r.message.gross_weight),
			net_weight: flt(r.message.metal_and_finding_weight),
		});
		frm.refresh_field("refining_serial_no_detail");
	} catch (error) {
		console.error(error);
	}
}

function filter_fields_based_on_company(frm, field_name) {
	frm.set_query(field_name, function () {
		return {
			filters: { company: frm.doc.company, is_group: 0 },
		};
	});
}

function filter_items(frm, table_name, varient) {
	let field = "item";
	if (varient == "M") {
		field = "item_code";
	}
	frm.set_query(field, table_name, function (doc, cdt, cdn) {
		let d = locals[cdt][cdn];
		return {
			filters: {
				variant_of: varient,
			},
		};
	});
}

function get_source_warehouse(frm, type, name) {
	let filters = {};
	filters[type] = name;
	frappe.db.get_value("Warehouse", filters, "name").then((r) => {
		console.log(r.message.name); // Open
		if (r.message.name) {
			frm.set_value("source_warehouse", r.message.name);
		} else {
			frappe.throw(`No Warehouse Found for ${type}: ${name}`);
		}
	});
}

function transfer_dust_btn(frm) {
	if (frm.doc.refining_type == "Recovery Material") {
		if (!frm.doc.__islocal && !frm.doc.dust_received) {
			frm.add_custom_button(__("Transfer Dust"), () => {
				frm.call("create_dust_receive_entry").then((r) => {
					if (r.message) {
						frm.set_value("dust_received", 1);
						for (let field of [
							"refining_type",
							"dustname",
							"refining_department",
							"department",
							"operation",
							"employee",
						]) {
							frm.set_df_property(field, "read_only", 1);
							frm.save();
							frappe.msgprint(__("Dust Received in Refining"));
						}
					}
				});
			}).addClass("btn-primary");
		}
	} else {
		frm.remove_custom_button("Transfer Dust");
	}
}

function filter_recovery_source(frm, field_name, table_name) {
	frm.set_query(field_name, table_name, function (doc, cdt, cdn) {
		let d = locals[cdt][cdn];
		return {
			filters: {
				company: frm.doc.company,
			},
		};
	});
}
