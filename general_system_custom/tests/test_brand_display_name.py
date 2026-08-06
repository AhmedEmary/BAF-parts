from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBrandDisplayName(TransactionCase):
    """`display_name` uses `display_label` when set and falls back to `name`.

    `name` stays the technical key (default_code prefix, pricing column key,
    discount table key); `display_label` is the user-facing label.
    """

    def setUp(self):
        super().setUp()
        self.Brand = self.env['product.brand']

    def test_display_name_falls_back_to_name(self):
        brand = self.Brand.create({'name': 'BMW'})
        self.assertEqual(brand.display_name, 'BMW')

    def test_display_name_uses_display_label(self):
        brand = self.Brand.create({'name': 'JLR', 'display_label': 'Jaguar & Land Rover'})
        self.assertEqual(brand.display_name, 'Jaguar & Land Rover')
        self.assertEqual(brand.name, 'JLR')

    def test_display_name_recomputes_on_label_change(self):
        brand = self.Brand.create({'name': 'HON'})
        self.assertEqual(brand.display_name, 'HON')
        brand.display_label = 'Honda'
        self.assertEqual(brand.display_name, 'Honda')
        brand.display_label = False
        self.assertEqual(brand.display_name, 'HON')
