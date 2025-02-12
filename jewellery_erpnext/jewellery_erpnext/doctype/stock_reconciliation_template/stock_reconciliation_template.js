frappe.ui.form.on("Stock Reconciliation template", {
	day(frm) {
		var now = new Date();
		var currentTime = now.toLocaleTimeString();
		if (!frm.doc.date && frm.doc.day == "Every Day : Working") {
			frm.set_value("time", currentTime);
		} else if (frm.doc.day == "End of Month : Working") {
			frm.set_value("time", currentTime);
			// Calculate the end of the month date
			var endOfMonth = getEndOfMonth();
			frm.set_value("date", endOfMonth);
		} else if (frm.doc.day == "End of the Year : Working") {
			frm.set_value("time", currentTime);
			// Calculate the end of the year date
			var endOfYear = getEndOfYear();
			frm.set_value("date", endOfYear);
		}
	},
});

// Function to get the end of month date
function getEndOfMonth() {
	var today = new Date();
	var year = today.getFullYear();
	var month = today.getMonth() + 1; // Months are zero-indexed in JavaScript, so January is 0

	// Get the last day of the current month
	var lastDayOfMonth = new Date(year, month, 0).getDate();

	// Initialize the end of month date
	var endOfMonth = new Date(year, month - 1, lastDayOfMonth);

	// Check if end of month is a Sunday (day 0)
	if (endOfMonth.getDay() === 0) {
		// If it's a Sunday, decrement the date until it's a working day (not Sunday)
		while (endOfMonth.getDay() === 0 || endOfMonth.getDay() === 6) {
			// Skip Sunday (0) and Saturday (6)
			endOfMonth.setDate(endOfMonth.getDate() - 1);
		}
	}

	return endOfMonth;
}

// Function to get the end of year date
function getEndOfYear() {
	var today = new Date();
	var year = today.getFullYear();

	// Initialize the end of year date to December 31st of the current year
	var endOfYear = new Date(year, 11, 31);

	// Check if end of year is a Sunday (day 0)
	if (endOfYear.getDay() === 0) {
		// If it's a Sunday, decrement the date until it's a working day (not Sunday)
		while (endOfYear.getDay() === 0 || endOfYear.getDay() === 6) {
			// Skip Sunday (0) and Saturday (6)
			endOfYear.setDate(endOfYear.getDate() - 1);
		}
	}

	return endOfYear;
}
