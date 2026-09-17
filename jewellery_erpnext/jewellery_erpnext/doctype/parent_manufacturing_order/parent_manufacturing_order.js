// Copyright (c) 2023, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Parent Manufacturing Order", {
	setup(frm) {
		filter_departments(frm, "diamond_department");
		filter_departments(frm, "gemstone_department");
		filter_departments(frm, "finding_department");
		filter_departments(frm, "other_material_department");
		filter_departments(frm, "metal_department");
		var parent_fields = [
			["diamond_grade", "Diamond Grade"],
			["metal_colour", "Metal Colour"],
			["metal_purity", "Metal Purity"],
		];
		set_filters_on_parent_table_fields(frm, parent_fields);
	},
	refresh(frm) {
		if ((frm.doc.customer || frm.doc.ref_customer) && frm.doc.diamond_quality) {
			frm.set_query("diamond_grade", function () {
				return {
					query: "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.filters_query.get_diamond_grade",
					searchfield: "diamond_grade",
					filters: {
						// ref_customer has to travel with the customer: the server prefers it, and
						// set_auto_diamond_grade already resolves against it. Sending only customer
						// made the picker list grades the save would immediately replace.
						customer: frm.doc.customer,
						ref_customer: frm.doc.ref_customer,
						diamond_quality: frm.doc.diamond_quality,
						use_custom_diamond_grade: frm.doc.use_custom_diamond_grade ? 1 : 0,
						is_customer_diamond: frm.doc.is_customer_diamond ? 1 : 0,
						sales_type: frm.doc.sales_type,
					},
				};
			});
		}

		// Always control read-only dynamically
		frm.set_df_property("diamond_grade", "read_only", !frm.doc.use_custom_diamond_grade);

		apply_customer_details_visibility(frm);
		apply_grade_override_hint(frm);

		if (!frm.doc.__islocal) {
			frm.add_custom_button(__("Send For Customer Approval"), function () {
				frm.trigger("create_customer_transfer");
			});
		}
		if (frm.doc.docstatus == 1) {
			frm.add_custom_button(__("Create MWO"), function () {
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
					function (data) {
						// Run only after selecting reason
						frappe.call({
							method: "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order.create_mwo",
							args: {
								pmo: frm.doc.name,
								doc: frm.doc,
								reason: data.reason, // passing reason also
							},
							callback: function (r) {
								if (!r.exc) {
									frm.reload_doc();
								}
							},
						});
					},
					__("Select Reason"), // Dialog Title
					__("Submit") // Submit button label
				);
			});
		}
	},

	customer(frm) {
		set_auto_diamond_grade(frm);
	},

	diamond_quality(frm) {
		set_auto_diamond_grade(frm);
	},

	is_customer_diamond(frm) {
		set_auto_diamond_grade(frm);
		apply_grade_override_hint(frm);
	},

	use_custom_diamond_grade(frm) {
		frm.set_df_property("diamond_grade", "read_only", !frm.doc.use_custom_diamond_grade);
		set_auto_diamond_grade(frm);
		apply_grade_override_hint(frm);
	},

	create_customer_transfer: function (frm) {
		frm.call({
			doc: frm.doc,
			method: "send_to_customer_for_approval",
			freeze: true,
			freeze_message: __("Transfering to Central...."),
			callback: (r) => {
				if (!r.exc) {
					frappe.msgprint(__("Manufacturing Entry has been created."));
					frm.refresh();
				}
			},
		});
	},
	sales_order_item: function (frm) {
		frappe.call({
			method: "jewellery_erpnext.jewellery_erpnext.doctype.production_order.production_order.get_item_code",
			args: {
				sales_order_item: frm.doc.sales_order_item,
			},
			type: "GET",
			callback: function (r) {
				console.log(r.message);
				frm.doc.item_code = r.message;
				frm.set_value("item_code", r.message);
				refresh_field("item_code");
				frm.trigger("item_code");
			},
		});
	},
	after_workflow_action(frm) {
		const onHoldStates = ["On Hold"];

		if (onHoldStates.includes(frm.doc.workflow_state)) {
			frappe.prompt(
				[
					{
						label: "Reason For Hold",
						fieldname: "update_reason",
						fieldtype: "Data",
						reqd: 1,
					},
				],
				(values) => {
					if (!values.update_reason) {
						frappe.msgprint(__("Please provide a reason for putting the order on hold."));
						return;
					}

					frappe.call({
						method: "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order.add_hold_comment",
						args: {
							doctype: frm.doctype,
							docname: frm.docname,
							reason: values.update_reason,
						},
						callback: function (r) {
							if (!r.exc) {
								frappe.msgprint(__("Comment added successfully."));
								frm.refresh();
							} else {
								frappe.msgprint(__("Failed to add comment. Please try again."));
							}
						},
						error: function (err) {
							console.error("Error in frappe.call:", err);
							frappe.msgprint(
								__("An error occurred while adding the comment. Please try again.")
							);
						},
					});
				}
			);
		}
	},
});

// The section break plus every field in it: hiding the break alone leaves the fields reachable if
// anything later re-renders them, and the list is short enough to be exact.
const CUSTOMER_DETAIL_FIELDS = [
	"customer_details_section",
	"is_customer_gold",
	"is_customer_diamond",
	"is_customer_gemstone",
	"is_customer_material",
];

// Sales Type -> the is_customer_diamond it implies, mirroring SALES_TYPE_CUSTOMER_DIAMOND in
// doc_events/filters_query.py. Only used to explain an empty dropdown; the server decides the list.
const SALES_TYPE_CUSTOMER_DIAMOND = { Outright: 0, Outwork: 1 };

// Cosmetic only. The values still reach the browser and the REST API -- this keeps the section off
// the form, it does not stop a non-manager writing those fields another way.
function apply_customer_details_visibility(frm) {
	// Set both ways, not just to 1: df properties live on the client-side doctype meta and
	// persist across forms for the session, so a one-way toggle leaks into the next form.
	const hidden = frappe.user.has_role("System Manager") ? 0 : 1;
	CUSTOMER_DETAIL_FIELDS.forEach((field) => {
		frm.set_df_property(field, "hidden", hidden);
	});
}

// An Outright/Outwork PMO whose Is Customer Diamond disagrees with it gets an empty grade list
// from the server. Say why, or the override looks broken rather than blocked.
function apply_grade_override_hint(frm) {
	// Sales Type is a Link, so its value is whatever Sales Type records exist -- test for the
	// two values this map holds rather than for "not undefined", which a name like "constructor"
	// would satisfy off Object.prototype.
	const expected = SALES_TYPE_CUSTOMER_DIAMOND[frm.doc.sales_type];
	const has_rule = expected === 0 || expected === 1;
	const mismatch =
		frm.doc.use_custom_diamond_grade && has_rule && expected !== (frm.doc.is_customer_diamond ? 1 : 0);

	frm.set_df_property(
		"diamond_grade",
		"description",
		mismatch
			? __("No grades to choose from: a {0} order expects Is Customer Diamond to be {1}.", [
					frm.doc.sales_type,
					expected ? __("ticked") : __("unticked"),
			  ])
			: ""
	);
}

// Must never be called from refresh: frm.set_value marks the form dirty, so resolving the grade on
// load makes a freshly opened record show as unsaved. Only run it when the user changes an input;
// on save the controller resolves the same value through resolve_diamond_grade.
function set_auto_diamond_grade(frm) {
	if (frm.doc.use_custom_diamond_grade) {
		return;
	}
	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.filters_query.get_auto_diamond_grade",
		args: {
			customer: frm.doc.customer,
			ref_customer: frm.doc.ref_customer,
			diamond_quality: frm.doc.diamond_quality,
			is_customer_diamond: frm.doc.is_customer_diamond ? 1 : 0,
		},
		callback: function (r) {
			if (r.message) {
				frm.set_value("diamond_grade", r.message);
			}
		},
	});
}

function filter_departments(frm, field_name) {
	frm.set_query(field_name, function () {
		return {
			filters: {
				company: frm.doc.company,
			},
		};
	});
}

function set_filters_on_parent_table_fields(frm, fields) {
	fields.map(function (field) {
		frm.set_query(field[0], function (doc) {
			return {
				query: "jewellery_erpnext.query.item_attribute_query",
				filters: { item_attribute: field[1] },
			};
		});
	});
}
function set_html(frm) {
	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order.get_stock_summary",
		args: {
			pmo_name: frm.doc.name,
		},
		callback: function (r) {
			frm.get_field("stock_summery").$wrapper.html(r.message);
		},
	});

	frappe.call({
		method: "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order.get_linked_stock_entries",
		doc: frm.doc,
		args: {
			pmo_name: frm.doc.name,
		},
		callback: function (r) {
			frm.get_field("stock_entry_details").$wrapper.html(r.message);
		},
	});
}
