from odoo.exceptions import UserError
from odoo.tests.common import tagged

from .common import LexcomCommon, PASSWORD, USERNAME


@tagged("post_install", "-at_install")
class TestLexcomSettings(LexcomCommon):
    """Credentials entered in Settings must reach res.company on every save path."""

    def _settings(self, **vals):
        return self.env["res.config.settings"].create(vals)

    def test_top_save_persists_typed_password(self):
        # The standard Save button and the "save your changes?" dialog both run
        # execute(); a typed password used to be dropped on that path.
        settings = self._settings(lexcom_password="typed-then-saved")
        settings.execute()

        self.assertTrue(self.company._lexcom_check_password("typed-then-saved"))
        self.assertFalse(self.company._lexcom_check_password(PASSWORD))
        self.assertFalse(settings.lexcom_password, "plaintext must not linger")

    def test_top_save_without_password_keeps_stored_hash(self):
        self._settings().execute()

        self.assertTrue(self.company._lexcom_check_password(PASSWORD))

    def test_save_credentials_button_persists_username_and_password(self):
        settings = self._settings(
            lexcom_username="new-user", lexcom_password="new-pass",
        )
        settings.action_lexcom_set_password()

        self.assertEqual(self.company.lexcom_username, "new-user")
        self.assertTrue(self.company._lexcom_check_password("new-pass"))
        self.assertFalse(settings.lexcom_password)

    def test_save_credentials_button_requires_password(self):
        settings = self._settings(lexcom_username=USERNAME)
        with self.assertRaises(UserError):
            settings.action_lexcom_set_password()

        self.assertTrue(self.company._lexcom_check_password(PASSWORD))

    def test_save_credentials_button_requires_username(self):
        settings = self._settings(lexcom_username=False, lexcom_password="x-pass")
        with self.assertRaises(UserError):
            settings.action_lexcom_set_password()

        self.assertTrue(self.company._lexcom_check_password(PASSWORD))

    def test_clear_password(self):
        self._settings().action_lexcom_clear_password()

        self.assertFalse(self.company.sudo().lexcom_password_hash)
        self.assertFalse(self._settings().lexcom_password_set)

    def test_password_set_flag(self):
        self.assertTrue(self._settings().lexcom_password_set)

    def test_brand_code_is_editable_on_brand_form_and_list(self):
        Brand = self.env["product.brand"]
        for view_type in ("form", "list"):
            arch = Brand.get_view(view_type=view_type)["arch"]
            self.assertIn('name="lexcom_code"', arch, view_type)

    def test_brand_code_offers_only_appendix_a_codes(self):
        codes = dict(
            self.env["product.brand"]._fields["lexcom_code"].selection
        )
        self.assertIn("Landrover", codes)
        self.assertIn("Mercedes-Benz", codes)
        self.assertNotIn("LandRover", codes)
