"""The patch that stops three DB Client Scripts from changing documents on open.

Client Script records are simulated: the scripts exist only in each site's database.
"""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from jewellery_erpnext.patches import fix_client_scripts_on_open as fix


def wrap(block):
	return "frappe.ui.form.on('X', {\n\trefresh(frm){\n" + block + "\t},\n});\n"


class TestApplyEdits(TestCase):
	def test_prod_bom_block_is_replaced_and_purity_no_longer_set_on_refresh(self):
		script, applied = fix.apply_edits(
			wrap(fix.BOM_PURITY_ON_REFRESH), fix.EDITS["BOM-Form-94"]
		)
		self.assertEqual(applied, 1)
		self.assertNotIn('set_value("metal_purity"', script)
		self.assertIn(fix.BOM_PURITY_NOTE, script)

	def test_uat_bom_block_with_22kt_commented_is_replaced(self):
		script, applied = fix.apply_edits(
			wrap(fix.BOM_PURITY_ON_REFRESH_22KT_COMMENTED), fix.EDITS["BOM-Form-94"]
		)
		self.assertEqual(applied, 1)
		self.assertNotIn('set_value("metal_purity", "75.4")', script)

	def test_second_run_changes_nothing(self):
		for name, edits in fix.EDITS.items():
			with self.subTest(name=name):
				once, _ = fix.apply_edits(wrap(edits[0][0]), edits)
				twice, applied = fix.apply_edits(once, edits)
				self.assertEqual((twice, applied), (once, 0))

	def test_edited_script_is_left_alone(self):
		edited = wrap(fix.BOM_PURITY_ON_REFRESH.replace("75.4", "75.5"))
		self.assertEqual(fix.apply_edits(edited, fix.EDITS["BOM-Form-94"]), (edited, 0))

	def test_block_occurring_twice_is_left_alone(self):
		old = fix.EDITS["Item-Form-118"][0][0]
		doubled = wrap(old + old)
		self.assertEqual(
			fix.apply_edits(doubled, fix.EDITS["Item-Form-118"]), (doubled, 0)
		)

	def test_item_form_118_runs_only_for_new_items(self):
		script, _ = fix.apply_edits(
			wrap(fix.EDITS["Item-Form-118"][0][0]), fix.EDITS["Item-Form-118"]
		)
		self.assertIn(
			"if(frm.is_new() && frm.doc.variant_of && frm.doc.item_code){", script
		)

	def test_item_image_signs_through_a_cache(self):
		script, _ = fix.apply_edits(
			wrap(fix.EDITS["Item Image"][0][0]), fix.EDITS["Item Image"]
		)
		self.assertIn("signed_url_cache.get(value)", script)
		self.assertIn("async function fetch_signed_url(value) {", script)


class TestExecute(TestCase):
	def run_patch(self, scripts):
		docs = {
			name: SimpleNamespace(script=text, save=Mock())
			for name, text in scripts.items()
		}
		db = SimpleNamespace(exists=lambda doctype, name: name in docs)
		with patch.object(fix.frappe, "db", db):
			with patch.object(
				fix.frappe, "get_doc", side_effect=lambda doctype, name: docs[name]
			):
				with patch("builtins.print"):
					fix.execute()
		return docs

	def test_updates_matching_scripts_and_skips_missing_ones(self):
		original = wrap(fix.BOM_PURITY_ON_REFRESH)
		docs = self.run_patch({"BOM-Form-94": original})
		doc = docs["BOM-Form-94"]
		doc.save.assert_called_once_with(ignore_permissions=True)
		self.assertNotEqual(doc.script, original)

	def test_differing_script_is_not_saved(self):
		docs = self.run_patch({"Item-Form-118": "frappe.ui.form.on('Item', {});\n"})
		docs["Item-Form-118"].save.assert_not_called()
