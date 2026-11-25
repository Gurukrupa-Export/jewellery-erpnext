"""How the Customer Goods flow shows numbers: three decimals, padded.

Weights, purities, rates and amounts in this flow are kept and shown at 3 decimals, the
site's float and currency precision on kg-gk. ``frappe.format_value`` would reach the
same text but needs a field; messages built in Python only have the number.
"""

from frappe.utils import flt

PRECISION = 3


def fmt3(value):
	"""``value`` rounded to 3 decimals and printed with all three: 1.5 -> "1.500"."""
	return f"{flt(value, PRECISION):.{PRECISION}f}"
