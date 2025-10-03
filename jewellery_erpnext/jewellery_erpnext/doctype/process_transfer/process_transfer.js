// Copyright (c) 2025, Nirali and contributors
// For license information, please see license.txt

frappe.ui.form.on("Process Transfer", {
    // refresh: function (frm) {
    // 	set_html(frm);
    // },
    onload(frm) {
        // frm.fields_dict["process_transfer_operation"].grid.add_new_row = false;
        // $(frm.fields_dict["process_transfer_operation"].grid.wrapper).find(".grid-add-row").hide();


       frm.set_df_property("current_process", "hidden", 1);
       frm.set_df_property("next_process", "hidden", 1);


        frm.set_query("department", function () {
            return {
                filters: {
                    name: ["in", ["Swastik - GEPL", "Rudraksha - GEPL", "Trishul - GEPL", "Om - GEPL"]]
                }
            };
        });
    },




    department(frm) {
        const next = ["Swastik - GEPL", "Rudraksha - GEPL", "Trishul - GEPL", "Om - GEPL"]

        if (next.includes(frm.doc.department)) {
            // Show the field
            frm.set_df_property("current_process", "hidden", 0);
            frm.set_df_property("next_process", "hidden", 0);

            // Apply filter based on department
            frm.set_query("current_process", function () {
                return {
                    filters: {
                        department: frm.doc.department
                    }
                };
            });

            frm.set_query("next_process", function () {
                return {
                    filters: {
                        department: frm.doc.department
                    }
                };
            });

            frm.refresh_field("current_process");
            frm.refresh_field("next_process");
        } else {
            console.log("Hiding current_process, not in allowed list");

            // Hide the field
            frm.set_df_property("current_process", "hidden", 1);
            frm.set_df_property("next_process", "hidden", 1);


            // Clear its value
            frm.set_value("current_process", "");
            frm.set_value("next_process", "");
            
        }
        // if (frm.doc.next_process) {
        //     frm.set_value("next_process", "");
        // }
        // if (frm.doc.department) {
        //     frm.set_query("next_process", function () {
        //         return {
        //             filters: {
        //                 department: frm.doc.department
        //             }
        //         };
        //     });
        // } else {
        //     frm.set_value("next_process", "");
        // }
    },

    get_operations(frm) {
        if (!frm.doc.department) {
            frappe.throw(__("Please select current department first"));
        }
        var query_filters = {
            company: frm.doc.company,
        };
        if (frm.doc.type == "Issue") {
            query_filters["department_ir_status"] = ["not in", ["In-Transit", "Revert"]];
            query_filters["status"] = ["in", ["Not Started"]];
            query_filters["employee"] = ["is", "not set"];
            query_filters["subcontractor"] = ["is", "not set"];
        } else {
            query_filters["department_ir_status"] = "In-Transit";
            query_filters["department"] = frm.doc.department;
        }
        if (frm.doc.next_department && frm.doc.is_finding == 0) {
            query_filters["is_finding"] = 0;
        }
        erpnext.utils.map_current_doc({
            method: "jewellery_erpnext.jewellery_erpnext.doctype.process_transfer.process_transfer.get_manufacturing_operations",
            source_doctype: "Manufacturing Operation",
            target: frm,
            setters: {
                manufacturing_work_order: undefined,
                company: frm.doc.company || undefined,
                department: frm.doc.department,
            },
            get_query_filters: query_filters,
            size: "extra-large",
        });
    },

    scan_mwo(frm) {
        if (frm.doc.scan_mwo) {
            frm.doc.process_transfer_operation.forEach(function (item) {
                if (item.manufacturing_work_order == frm.doc.scan_mwo)
                    frappe.throw(
                        __("{0} Manufacturing Work Order already exists", [frm.doc.scan_mwo])
                    );
            });
            // if (frm.doc.department_ir_operation.length > 30) {
            // 	frappe.throw(__("Only 30 MOP allowed in one document"));
            // }
            if (!frm.doc.department) {
                frappe.throw(__("Please select current department first"));
            }
            var query_filters = {
                company: frm.doc.company,
                manufacturing_work_order: frm.doc.scan_mwo,
                department: frm.doc.department,
                department_process: frm.doc.department_process, // check fieldname!
                status: "Not Started"
            };

            // console.log(query_filters)
            // if (frm.doc.type == "Issue") {
            // 	query_filters["department_ir_status"] = ["not in", ["In-Transit", "Revert"]];
            // 	query_filters["status"] = ["in", ["Not Started"]];
            // 	query_filters["employee"] = ["is", "not set"];
            // 	query_filters["subcontractor"] = ["is", "not set"];
            // } else {
            // 	query_filters["department_ir_status"] = ["in", ["In-Transit", "Received"]];
            // }
            // query_filters["status"] = ["in", ["Not Started"]];

            // if (frm.doc.next_department && frm.doc.is_finding == 0) {
            // 	query_filters["is_finding"] = 0;
            // }

            frappe.db
                .get_value("Manufacturing Operation", query_filters, [
                    "name",
                    "manufacturing_work_order",
                    "status",
                    "gross_wt",
                    "diamond_wt",
                    "net_wt",
                    "finding_wt",
                    "diamond_pcs",
                    "gemstone_pcs",
                    "gemstone_wt",
                    "other_wt",
                    "previous_mop",
                ])
                .then((r) => {
                    let values = r.message;
                    frappe.db
                        .get_value("Manufacturing Operation", values.previous_mop, [
                            "gross_wt",
                            "diamond_wt",
                            "net_wt",
                            "finding_wt",
                            "diamond_pcs",
                            "gemstone_pcs",
                            "gemstone_wt",
                            "other_wt",
                            "received_gross_wt",
                        ])
                        .then((v) => {
                            if (values.manufacturing_work_order) {
                                let gr_wt = 0;
                                if (values.gross_wt > 0) {
                                    gr_wt = values.gross_wt;
                                } else if (v.message.received_gross_wt > 0 || v.message.gross_wt) {
                                    if (v.message.received_gross_wt > 0) {
                                        gr_wt = v.message.received_gross_wt;
                                    } else if (v.message.gross_wt > 0) {
                                        gr_wt = v.message.gross_wt;
                                    }
                                }

                                let row = frm.add_child("process_transfer_operation", {
                                    manufacturing_work_order: values.manufacturing_work_order,
                                    manufacturing_operation: values.name,
                                    status: values.status,
                                    gross_wt: gr_wt,
                                    diamond_wt:
                                        values.diamond_wt > 0
                                            ? values.diamond_wt
                                            : v.message.diamond_wt,
                                    net_wt: values.net_wt > 0 ? values.net_wt : v.message.net_wt,
                                    finding_wt:
                                        values.finding_wt > 0
                                            ? values.finding_wt
                                            : v.message.finding_wt,
                                    gemstone_wt:
                                        values.gemstone_wt > 0
                                            ? values.gemstone_wt
                                            : v.message.gemstone_wt,
                                    other_wt:
                                        values.other_wt > 0 ? values.other_wt : v.message.other_wt,
                                    diamond_pcs:
                                        values.diamond_pcs > 0
                                            ? values.diamond_pcs
                                            : v.message.diamond_pcs,
                                    gemstone_pcs:
                                        values.gemstone_pcs > 0
                                            ? values.gemstone_pcs
                                            : v.message.gemstone_pcs,
                                });
                                frm.refresh_field("process_transfer_operation");
                            } else {
                                frappe.throw(__("No Manufacturing Operation Found"));
                            }
                        });
                    frm.set_value("scan_mwo", "");
                });
        }
    },
});

