import frappe
from frappe.tests import IntegrationTestCase
from unittest.mock import patch, MagicMock
from jewellery_erpnext.jewellery_erpnext.doctype.diamond_conversion.diamond_conversion import validate_purity


class TestDiamondConversion(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch("frappe.db.get_all")
	def test_purity_validation_success_same_purity(self, mock_get_all):
		doc = MagicMock()
		doc.manufacturer = "Test Manufacturer"
		
		source_row = MagicMock()
		source_row.item_code = "ITEM-VVS"
		doc.sc_source_table = [source_row]
		
		target_row = MagicMock()
		target_row.item_code = "ITEM-VVS"
		doc.sc_target_table = [target_row]
		
		def get_all_side_effect(doctype, *args, **kwargs):
			if doctype == "Diamond Conversion Purity":
				return []
			if doctype == "Item Variant Attribute":
				return [
					frappe._dict({"parent": "ITEM-VVS", "attribute_value": "VVS"})
				]
			return []
		mock_get_all.side_effect = get_all_side_effect
		
		# Should pass without throwing an error
		validate_purity(doc)

	@patch("frappe.db.get_all")
	def test_purity_validation_success_allowed_mapping(self, mock_get_all):
		doc = MagicMock()
		doc.manufacturer = "Test Manufacturer"
		
		source_row = MagicMock()
		source_row.item_code = "ITEM-VVS"
		doc.sc_source_table = [source_row]
		
		target_row = MagicMock()
		target_row.item_code = "ITEM-VS"
		doc.sc_target_table = [target_row]
		
		def get_all_side_effect(doctype, *args, **kwargs):
			if doctype == "Diamond Conversion Purity":
				return [frappe._dict({"from_purity": "VVS", "to_purity": "VS"})]
			if doctype == "Item Variant Attribute":
				return [
					frappe._dict({"parent": "ITEM-VVS", "attribute_value": "VVS"}),
					frappe._dict({"parent": "ITEM-VS", "attribute_value": "VS"})
				]
			return []
		mock_get_all.side_effect = get_all_side_effect
		
		# Should pass without throwing an error
		validate_purity(doc)

	@patch("frappe.db.get_all")
	def test_purity_validation_failure_not_allowed(self, mock_get_all):
		doc = MagicMock()
		doc.manufacturer = "Test Manufacturer"
		
		source_row = MagicMock()
		source_row.item_code = "ITEM-VVS"
		doc.sc_source_table = [source_row]
		
		target_row = MagicMock()
		target_row.item_code = "ITEM-SI"
		doc.sc_target_table = [target_row]
		
		def get_all_side_effect(doctype, *args, **kwargs):
			if doctype == "Diamond Conversion Purity":
				return [frappe._dict({"from_purity": "VVS", "to_purity": "VS"})]
			if doctype == "Item Variant Attribute":
				return [
					frappe._dict({"parent": "ITEM-VVS", "attribute_value": "VVS"}),
					frappe._dict({"parent": "ITEM-SI", "attribute_value": "SI"})
				]
			return []
		mock_get_all.side_effect = get_all_side_effect
		
		self.assertRaises(frappe.ValidationError, validate_purity, doc)

	@patch("frappe.db.get_all")
	def test_purity_validation_mixed_batch(self, mock_get_all):
		doc = MagicMock()
		doc.manufacturer = "Test Manufacturer"
		
		src1, src2 = MagicMock(), MagicMock()
		src1.item_code, src2.item_code = "ITEM-VVS", "ITEM-VS"
		doc.sc_source_table = [src1, src2]
		
		tgt1, tgt2 = MagicMock(), MagicMock()
		tgt1.item_code, tgt2.item_code = "ITEM-VVS", "ITEM-SI"
		doc.sc_target_table = [tgt1, tgt2]
		
		def get_all_side_effect(doctype, *args, **kwargs):
			if doctype == "Diamond Conversion Purity":
				return [frappe._dict({"from_purity": "VS", "to_purity": "SI"})]
			if doctype == "Item Variant Attribute":
				return [
					frappe._dict({"parent": "ITEM-VVS", "attribute_value": "VVS"}),
					frappe._dict({"parent": "ITEM-VS", "attribute_value": "VS"}),
					frappe._dict({"parent": "ITEM-SI", "attribute_value": "SI"})
				]
			return []
		mock_get_all.side_effect = get_all_side_effect
		
		# Should pass without throwing an error
		validate_purity(doc)

	@patch("frappe.db.get_all")
	def test_purity_validation_missing_grade(self, mock_get_all):
		doc = MagicMock()
		doc.manufacturer = "Test Manufacturer"
		
		source_row = MagicMock()
		source_row.idx = 1
		source_row.item_code = "ITEM-NOGRADE"
		doc.sc_source_table = [source_row]
		doc.sc_target_table = []
		
		def get_all_side_effect(doctype, *args, **kwargs):
			if doctype == "Diamond Conversion Purity":
				return []
			if doctype == "Item Variant Attribute":
				return [] # Return empty for NOGRADE
			return []
		mock_get_all.side_effect = get_all_side_effect
		
		# Should raise validation error
		self.assertRaises(frappe.ValidationError, validate_purity, doc)

	def tearDown(self):
		return super().tearDown()
