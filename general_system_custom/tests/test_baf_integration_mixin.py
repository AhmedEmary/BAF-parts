from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger

_MIXIN_LOGGER = "odoo.addons.general_system_custom.models.baf_integration_mixin"


@tagged("post_install", "-at_install")
class TestBafIntegrationMixin(TransactionCase):
    """Direct coverage for the helpers extracted out of alzura_integration.

    They were previously exercised only through the Alzura importer, so a
    behaviour change during the extraction could only surface as a confusing
    failure somewhere downstream.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mixin = cls.env["baf.integration.mixin"]
        cls.company = cls.env.company

    def _make_sale_tax(self, name, amount):
        return self.env["account.tax"].create({
            "name": name,
            "amount": amount,
            "amount_type": "percent",
            "type_tax_use": "sale",
            "company_id": self.company.id,
        })

    # ── _baf_tax_ids_for_rate ────────────────────────────────────────────────

    def test_rate_matches_single_tax(self):
        """0.19 resolves to the one 19 % sale tax of the company."""
        tax = self._make_sale_tax("BAF Test 19", 19.0)
        found = self.mixin._baf_tax_ids_for_rate(self.company, 0.19)
        self.assertEqual(found, tax)

    def test_company_default_wins_among_equal_rates(self):
        """With several taxes at the same rate, the company default is used.

        Search order would otherwise decide which tax an imported order posts
        to, which is how an order silently lands on '19% EU D' instead of '19%'.
        """
        first = self._make_sale_tax("BAF Test 19 A", 19.0)
        second = self._make_sale_tax("BAF Test 19 B", 19.0)
        self.company.sudo().account_sale_tax_id = second
        found = self.mixin._baf_tax_ids_for_rate(self.company, 0.19)
        self.assertEqual(found, second)
        self.assertNotEqual(found, first)

    @mute_logger(_MIXIN_LOGGER)
    def test_no_matching_tax_returns_empty(self):
        """An unmatched rate returns an empty recordset, never a wrong tax."""
        found = self.mixin._baf_tax_ids_for_rate(self.company, 0.0777)
        self.assertFalse(found)
        self.assertEqual(found._name, "account.tax")

    def test_none_rate_returns_empty(self):
        """A missing rate is not an error; it leaves the product default."""
        self.assertFalse(self.mixin._baf_tax_ids_for_rate(self.company, None))

    # ── _baf_get_or_create_service_product ───────────────────────────────────

    def test_service_product_get_or_create_is_idempotent(self):
        first = self.mixin._baf_get_or_create_service_product(
            "BAF-TEST-CHARGE", "BAF Test Charge"
        )
        second = self.mixin._baf_get_or_create_service_product(
            "BAF-TEST-CHARGE", "BAF Test Charge"
        )
        self.assertEqual(first, second)
        self.assertEqual(first.type, "service")
        self.assertFalse(first.purchase_ok)
        self.assertEqual(
            self.env["product.product"].search_count(
                [("default_code", "=", "BAF-TEST-CHARGE")]
            ),
            1,
        )

    # ── _baf_country_by_code ─────────────────────────────────────────────────

    def test_country_by_code_matches(self):
        self.assertEqual(
            self.mixin._baf_country_by_code("DE"),
            self.env.ref("base.de"),
        )

    def test_country_by_code_is_case_insensitive(self):
        self.assertEqual(
            self.mixin._baf_country_by_code("de"),
            self.env.ref("base.de"),
        )

    def test_country_by_code_unknown_and_empty(self):
        self.assertFalse(self.mixin._baf_country_by_code("QQ"))
        self.assertFalse(self.mixin._baf_country_by_code(False))
