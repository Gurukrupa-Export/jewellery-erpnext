frappe.listview_settings['Parent Manufacturing Order'] = {
    onload: function(listview) {
        listview.page.add_inner_button('PMO PMO', () => {
            window.open('/pmo_home', '_blank');
        });
    }
};
