"""How the Customer Goods flow shows numbers: three decimals, padded, grouped like Frappe forms.

Weights, purities, rates and amounts in this flow are kept and shown at 3 decimals, the
site's float and currency precision on kg-gk. ``frappe.format_value`` would reach the
same text but needs a field; messages built in Python only have the number.
"""

from frappe.utils import flt, fmt_money, get_number_format
from frappe.utils.number_format import NumberFormat

PRECISION = 3

#: What ``frappe.locale.get_number_format`` itself falls back to.
DEFAULT_NUMBER_FORMAT = "#,###.##"


def fmt3(value):
	"""``value`` rounded to 3 decimals and printed with all three, grouped by the site's
	number format: 1.5 -> "1.500", 12924.4 -> "12,924.400" ("#,##,###.##": "12,34,567.891").

	``fmt_money`` groups the digits but keeps only 2 decimals when the value has fewer than 3
	("1.50"), so the fraction is padded back to 3 here. A format with no decimal separator
	("#,###") prints the plain number, never one with its decimals dropped.
	"""
	# ``or 0.0`` also turns a -0.0 (what -0.0004 rounds to) into 0.0: never "-0.000".
	value = flt(value, PRECISION) or 0.0
	number_format = _number_format()
	separator = number_format.decimal_separator
	if not separator:
		return f"{value:.{PRECISION}f}"
	text = fmt_money(value, precision=PRECISION, format=number_format.string)
	whole, _sep, fraction = text.partition(separator)
	return f"{whole}{separator}{fraction[:PRECISION].ljust(PRECISION, '0')}"


def _number_format():
	"""The number format Frappe formats with (the user's Language, else System Settings).

	Most callers are building a refusal: if the format cannot be read, the message is still
	built, in Frappe's default format, rather than replaced by the lookup's own error.
	"""
	try:
		return get_number_format()
	except Exception:
		return NumberFormat.from_string(DEFAULT_NUMBER_FORMAT)
