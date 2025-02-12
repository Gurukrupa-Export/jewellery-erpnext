import frappe
from frappe import _
from frappe.utils import get_link_to_form


def validate(self, method):
	pass
	# update_item_uom_conversion(self)


def update_item_uom_conversion_from_diamond():
	data = frappe.get_all("Diamond Weight")
	for row in data:
		diamond_weight_doc = frappe.get_doc("Diamond Weight", row.name)
		attribute_filters = {
			"Diamond Type": diamond_weight_doc.diamond_type,
			"Stone Shape": diamond_weight_doc.stone_shape,
			"Diamond Sieve Size": diamond_weight_doc.diamond_sieve_size,
		}
		item_list = get_item_codes_by_attributes(attribute_filters)
		if item_list:
			for item in item_list:
				doc = frappe.get_doc("Item", item)
				to_remove = [d for d in doc.uoms if d.uom == "Pcs"]
				for d in to_remove:
					doc.remove(d)
				doc.append("uoms", {"uom": "Pcs", "conversion_factor": diamond_weight_doc.weight})
				doc.save(ignore_permissions=True)
				frappe.msgprint(
					_("Item {0} conversion factor updated.").format(get_link_to_form("Item", doc.name))
				)


def get_item_codes_by_attributes(attribute_filters, template_item_code=None):
	items = []

	for attribute, values in attribute_filters.items():
		attribute_values = values

		if not isinstance(attribute_values, list):
			attribute_values = [attribute_values]

		if not attribute_values:
			continue

		wheres = []
		query_values = []
		for attribute_value in attribute_values:
			wheres.append("( attribute = %s and attribute_value = %s )")
			query_values += [attribute, attribute_value]

		attribute_query = " or ".join(wheres)

		if template_item_code:
			variant_of_query = "AND t2.variant_of = %s"
			query_values.append(template_item_code)
		else:
			variant_of_query = ""

		query = """
			SELECT
				t1.parent
			FROM
				`tabItem Variant Attribute` t1
			WHERE
				1 = 1
				AND (
					{attribute_query}
				)
				AND EXISTS (
					SELECT
						1
					FROM
						`tabItem` t2
					WHERE
						t2.name = t1.parent
						{variant_of_query}
				)
			GROUP BY
				t1.parent
			ORDER BY
				NULL
		""".format(
			attribute_query=attribute_query, variant_of_query=variant_of_query
		)

		item_codes = set([r[0] for r in frappe.db.sql(query, query_values)])  # nosemgrep
		items.append(item_codes)

	res = list(set.intersection(*items))

	return res
