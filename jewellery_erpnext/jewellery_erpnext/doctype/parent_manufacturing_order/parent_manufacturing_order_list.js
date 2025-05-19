frappe.listview_settings['Parent Manufacturing Order'] = {
	onload: function(listview) {
		listview.page.add_inner_button('PMO Print', () => {
			window.open('/pmo_home', '_blank');
		});
		listview.page.add_action_item(__('Bulk Submit & Process'), () => {
			const selected = listview.get_checked_items().map(row => row.name);
			if (!selected.length) {
				frappe.msgprint("Please select at least one PMO.");
				return;
			}
			frappe.call({
				method: "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order.bulk_submit_pmo",
				args: { pmo_names: selected },
				freeze: true,
				callback(r) {
					frappe.msgprint("Processed " + selected.length + " PMOs.");
					listview.refresh();
				}
			});
		});
	}
};
