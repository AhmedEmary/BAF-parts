"""Tests for ic_intercars — CSV cache, IC product creation, ordering.

All IC HTTP traffic is mocked at the ``InterCarsAPIClient`` boundary
(``IcBackend.get_client`` is patched), so the suite runs offline and
never touches the production account.
"""

import json

from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

_PO_LOGGER = 'odoo.addons.ic_intercars.models.purchase_order'
_PRODUCT_LOGGER = 'odoo.addons.ic_intercars.models.product_product'

# A minimal but realistic ProductInformation CSV. Note:
#   * semicolon separator, header row — exactly IC's format;
#   * row 2's IC_INDEX contains spaces (the OE-number formatting that
#     broke exact joins in production);
#   * decimal commas in PACKAGE_WEIGHT.
_SAMPLE_CSV = (
    "TOW_KOD;IC_INDEX;TEC_DOC;TEC_DOC_PROD;ARTICLE_NUMBER;MANUFACTURER;"
    "SHORT_DESCRIPTION;DESCRIPTION;BARCODES;PACKAGE_WEIGHT;PACKAGE_LENGTH;"
    "PACKAGE_WIDTH;PACKAGE_HEIGHT;CUSTOM_CODE;BLOCKED_RETURN\n"
    "ADDFFF;OP 520;OP 520;256;OP 520;FILTRON;Oil filter;"
    "Oil filter long descr;5904608005205;0,378;15,0;10,0;10,0;84212300;false\n"
    "G14740;61 13 1 359 287;61131359287;9999;61131359287;OE BMW;"
    "Battery clamp;Battery clamp for BMW;4010858481162;0,1;5,0;5,0;5,0;85369010;false\n"
    "BEF134;VAL231498;231498;21;231498;VALEO;Radiator;"
    "Engine cooling radiator;;;;;;87089135;true\n"
).encode()


class IcCase(TransactionCase):
    """Shared fixtures: an IC vendor + backend, no live HTTP."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.vendor = cls.env['res.partner'].create({
            'name': 'Inter Cars Test Vendor',
            'supplier_rank': 1,
        })
        cls.backend = cls.env['ic.backend'].create({
            'name': 'IC Test Backend',
            'vendor_id': cls.vendor.id,
            'client_id': 'test-client',
            'client_secret': 'test-secret',
            'token_url': 'https://token.invalid/oauth2/token',
            'currency_id': cls.env.ref('base.EUR').id,
            'ship_to': 'F17',
            'delivery_method': 'DIST',
            'payment_method': '14',
            'market': 'de',
        })

    def _mock_client(self, **overrides):
        """A MagicMock quacking like InterCarsAPIClient."""
        client = MagicMock()
        client.get_finances.return_value = {'orderingAllowed': True}
        client.submit_requisition.return_value = [{
            'id': 'UUID-1', 'requisitionId': 'RQ-1',
            'phaseCode': 'ACCEPTED', 'statusCode': 'NEW',
        }]
        client.confirm_requisition.return_value = [
            {'statusCode': 'CONFIRMED'},
        ]
        client.get_price.return_value = {'lines': []}
        # Default: the fixture SKU is in stock so the PO availability
        # pre-flight passes; tests exercising the block override this.
        client.get_stock.return_value = [
            {'sku': 'ADDFFF', 'availability': 5},
        ]
        for name, value in overrides.items():
            getattr(client, name).return_value = value
        return client


@tagged('post_install', '-at_install')
class TestProductInfoCache(IcCase):

    def test_bulk_load_csv(self):
        Info = self.env['ic.product.info']
        stats = Info.bulk_load_csv(_SAMPLE_CSV, replace=True)
        self.assertEqual(stats['rows'], 3)
        rec = Info.search([('tow_kod', '=', 'G14740')])
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec.manufacturer, 'OE BMW')

    def test_bulk_load_csv_rejects_unknown_columns(self):
        bad = b"TOW_KOD;SURPRISE_COLUMN\nX;Y\n"
        with self.assertRaises(UserError):
            self.env['ic.product.info'].bulk_load_csv(bad)

    def test_bulk_load_replaces_previous_snapshot(self):
        Info = self.env['ic.product.info']
        Info.bulk_load_csv(_SAMPLE_CSV, replace=True)
        Info.bulk_load_csv(_SAMPLE_CSV, replace=True)
        self.assertEqual(Info.search_count([]), 3,
                         "replace=True must not duplicate rows")

    def test_create_products_from_cache_rows(self):
        """Selected cache rows become normal aftermarket products;
        rows IC refuses to quote are skipped, never created at 0."""
        Info = self.env['ic.product.info']
        Info.bulk_load_csv(_SAMPLE_CSV, replace=True)
        rows = Info.search([('tow_kod', 'in', ['ADDFFF', 'BEF134'])])
        client = self._mock_client(get_price={'lines': [{
            'sku': 'ADDFFF',
            'price': {'customerPriceNet': 3.05, 'currencyCode': 'EUR'},
        }]})  # BEF134 deliberately unquoted
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            rows.action_create_products()
        made = self.env['product.product'].with_context(
            active_test=False).search([('ic_sku', '=', 'ADDFFF')])
        self.assertEqual(len(made), 1)
        tmpl = made.product_tmpl_id
        self.assertEqual(tmpl.part_quality, 'aftermarket')
        self.assertEqual(tmpl.manufacturer_id.name, 'FILTRON')
        self.assertEqual(tmpl.default_code, 'IC_ADDFFF')
        # comma-decimal weight parsed from the CSV TEXT column
        # (rounded to the Stock Weight precision, 2 digits)
        self.assertAlmostEqual(tmpl.weight, 0.38, places=2)
        self.assertFalse(
            self.env['product.product'].with_context(
                active_test=False).search([('ic_sku', '=', 'BEF134')]),
            "unquotable SKU must not be created")

    def test_bulk_load_accepts_optional_columns(self):
        """IC's docs list EPREL_LINK / UNIT / QUANTITY as optional feed
        columns — a feed configured with them must import cleanly."""
        csv_with_optional = (
            "TOW_KOD;IC_INDEX;TEC_DOC;TEC_DOC_PROD;ARTICLE_NUMBER;"
            "MANUFACTURER;SHORT_DESCRIPTION;DESCRIPTION;BARCODES;"
            "PACKAGE_WEIGHT;PACKAGE_LENGTH;PACKAGE_WIDTH;PACKAGE_HEIGHT;"
            "CUSTOM_CODE;BLOCKED_RETURN;EPREL_LINK;UNIT;QUANTITY\n"
            "OPT001;IDX 1;IDX1;1;IDX1;TESTBRAND;Thing;Long thing;"
            ";0,5;1;1;1;12345678;false;https://eprel.example/1;PCE;1\n"
        ).encode()
        stats = self.env['ic.product.info'].bulk_load_csv(
            csv_with_optional, replace=True)
        self.assertEqual(stats['rows'], 1)
        rec = self.env['ic.product.info'].search([('tow_kod', '=', 'OPT001')])
        self.assertEqual(rec.unit, 'PCE')
        self.assertEqual(rec.eprel_link, 'https://eprel.example/1')


@tagged('post_install', '-at_install')
class TestIcProductCreation(IcCase):

    _IC_DATA = {
        'sku': 'BEF134',
        'index': 'VAL231498',
        'brand': 'VALEO',
        'shortDescription': 'Radiator',
        'articleNumber': '231498',
        'eans': ['4010858481162'],
        'packageWeight': 2.5,
        'genericArticleReferences': [
            {'primary': True, 'genericArticleId': '231498'},
        ],
    }

    def test_lazy_create_sets_all_invariants(self):
        variant = self.env['product.product']._baf_find_or_create_ic(
            self.backend, dict(self._IC_DATA), price_net=42.5,
        )
        tmpl = variant.product_tmpl_id
        # BAF conventions: sku = raw IC SKU, explicit IC_ reference —
        # aftermarket parts carry no car-make brand, only a manufacturer.
        self.assertEqual(tmpl.sku, 'BEF134')
        self.assertEqual(tmpl.default_code, 'IC_BEF134')
        self.assertEqual(tmpl.ic_sku, 'BEF134')
        self.assertEqual(tmpl.part_quality, 'aftermarket')
        self.assertEqual(tmpl.manufacturer_id.name, 'VALEO')
        self.assertFalse(tmpl.brand, "no car-make brand on IC parts")
        self.assertEqual(
            tmpl.categ_id,
            self.env.ref('ic_intercars.product_categ_aftermarket'))
        self.assertEqual(variant.barcode, '4010858481162')
        # Customer price seeded from IC cost × markup (default 25 %),
        # so cart lines never fall back to Odoo's 1.0 default.
        self.assertAlmostEqual(tmpl.list_price, round(42.5 * 1.25, 2),
                               places=2)
        # Drop-ship route pinned.
        route = self.env.ref('stock_dropshipping.route_drop_shipping')
        self.assertIn(route, tmpl.route_ids)
        # Aftermarket ribbon assigned (customer-visible label).
        ribbon = self.env.ref('ic_intercars.ribbon_aftermarket')
        self.assertEqual(tmpl.website_ribbon_id, ribbon)
        # Supplierinfo carries the live IC cost in the backend currency.
        sup = self.env['product.supplierinfo'].search([
            ('product_tmpl_id', '=', tmpl.id),
            ('partner_id', '=', self.vendor.id),
        ])
        self.assertEqual(len(sup), 1)
        self.assertEqual(sup.price, 42.5)
        self.assertEqual(sup.currency_id, self.env.ref('base.EUR'))

    def test_lazy_create_is_idempotent(self):
        first = self.env['product.product']._baf_find_or_create_ic(
            self.backend, dict(self._IC_DATA), price_net=42.5,
        )
        second = self.env['product.product']._baf_find_or_create_ic(
            self.backend, dict(self._IC_DATA), price_net=39.0,
        )
        self.assertEqual(first, second, "same SKU must reuse the product")
        sup = self.env['product.supplierinfo'].search([
            ('product_tmpl_id', '=', first.product_tmpl_id.id),
            ('partner_id', '=', self.vendor.id),
        ])
        self.assertEqual(len(sup), 1, "no duplicate supplierinfo")
        self.assertEqual(sup.price, 39.0, "cost refreshed on re-call")
        self.assertAlmostEqual(
            first.product_tmpl_id.list_price, round(39.0 * 1.25, 2),
            places=2, msg="customer price refreshed with the new cost")

    def test_lazy_create_requires_sku(self):
        with self.assertRaises(UserError):
            self.env['product.product']._baf_find_or_create_ic(
                self.backend, {'brand': 'X'}, price_net=1.0,
            )

    def test_lazy_create_never_hijacks_oem_with_stray_ic_sku(self):
        """The LR_LR172375 incident: a user typed an IC SKU into the
        wrong field on an OEM product. Materialisation must ignore
        that record and create a proper aftermarket product."""
        oem = self.env['product.template'].create({
            'name': 'Real OEM part', 'sku': 'LR172375',
            'list_price': 393.46,
            'ic_sku': 'BEF134',       # stray value, wrong field
            'part_quality': 'oem',    # still an OEM part
        })
        variant = self.env['product.product']._baf_find_or_create_ic(
            self.backend, dict(self._IC_DATA), price_net=10.0,
        )
        self.assertNotEqual(variant.product_tmpl_id, oem,
                            "must not reuse the OEM template")
        self.assertEqual(oem.list_price, 393.46, "OEM price untouched")
        self.assertEqual(oem.part_quality, 'oem')
        self.assertFalse(self.env['product.supplierinfo'].search([
            ('product_tmpl_id', '=', oem.id),
            ('partner_id', '=', self.vendor.id),
        ]), "no IC supplierinfo may be attached to the OEM product")

    def test_lazy_create_reactivates_archived_product(self):
        """Re-ordering an archived IC product must reactivate it, not
        crash on the unique default_code constraint."""
        variant = self.env['product.product']._baf_find_or_create_ic(
            self.backend, dict(self._IC_DATA), price_net=10.0,
        )
        variant.product_tmpl_id.action_archive()
        self.assertFalse(variant.product_tmpl_id.active)
        again = self.env['product.product']._baf_find_or_create_ic(
            self.backend, dict(self._IC_DATA), price_net=12.0,
        )
        self.assertEqual(again, variant)
        self.assertTrue(again.product_tmpl_id.active,
                        "archived product must be reactivated on re-order")

    def test_manufacturer_reused_across_products(self):
        """Two IC parts from the same manufacturer share one
        product.manufacturer record — no duplicates."""
        first = self.env['product.product']._baf_find_or_create_ic(
            self.backend, dict(self._IC_DATA), price_net=10.0,
        )
        # distinct sku + no EAN so the barcode-uniqueness check
        # doesn't trip on the shared fixture barcode
        other = dict(self._IC_DATA, sku='BEF135', eans=[])
        second = self.env['product.product']._baf_find_or_create_ic(
            self.backend, other, price_net=10.0,
        )
        self.assertEqual(first.manufacturer_id, second.manufacturer_id)
        self.assertEqual(
            self.env['product.manufacturer'].search_count(
                [('name', '=', 'VALEO')]), 1)

    def test_reuse_without_price_keeps_list_price(self):
        first = self.env['product.product']._baf_find_or_create_ic(
            self.backend, dict(self._IC_DATA), price_net=40.0,
        )
        before = first.product_tmpl_id.list_price
        self.assertGreater(before, 1.0)
        self.env['product.product']._baf_find_or_create_ic(
            self.backend, dict(self._IC_DATA), price_net=None,
        )
        self.assertEqual(first.product_tmpl_id.list_price, before,
                         "a priceless refresh must not clobber the price")

    def test_markup_param_is_honoured(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'baf.ic_markup_pct', '40')
        self.assertEqual(
            self.env['product.product']._baf_ic_list_price(100.0), 140.0)

    def test_invalid_markup_param_falls_back_to_default(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'baf.ic_markup_pct', 'not-a-number')
        self.assertEqual(
            self.env['product.product']._baf_ic_list_price(100.0), 125.0)

    def test_live_cost_returns_quote(self):
        client = self._mock_client(get_price={'lines': [{
            'sku': 'ADDFFF',
            'price': {'customerPriceNet': 3.05, 'currencyCode': 'EUR'},
        }]})
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            cost = self.env['product.product']._baf_ic_live_cost(
                self.backend, 'ADDFFF')
        self.assertEqual(cost, 3.05)

    def test_live_cost_raises_when_unquotable(self):
        client = self._mock_client(get_price={'lines': []})
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            with self.assertRaises(UserError):
                self.env['product.product']._baf_ic_live_cost(
                    self.backend, 'DEADBEEF')

    def test_live_cost_wraps_client_errors(self):
        client = self._mock_client()
        client.get_price.side_effect = UserError("IC error (400): ICF201")
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            with self.assertRaises(UserError) as ctx:
                self.env['product.product']._baf_ic_live_cost(
                    self.backend, 'DEADBEEF')
            self.assertIn('ICF201', str(ctx.exception))

    def test_lazy_create_backfills_route_on_existing(self):
        variant = self.env['product.product']._baf_find_or_create_ic(
            self.backend, dict(self._IC_DATA), price_net=10.0,
        )
        route = self.env.ref('stock_dropshipping.route_drop_shipping')
        variant.product_tmpl_id.route_ids = [(3, route.id)]
        self.assertNotIn(route, variant.product_tmpl_id.route_ids)
        self.env['product.product']._baf_find_or_create_ic(
            self.backend, dict(self._IC_DATA), price_net=10.0,
        )
        self.assertIn(route, variant.product_tmpl_id.route_ids,
                      "route must be re-pinned on reuse")


@tagged('post_install', '-at_install')
class TestPurchaseOrderRequisition(IcCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.product = cls.env['product.product']._baf_find_or_create_ic(
            cls.backend, {
                'sku': 'ADDFFF', 'brand': 'FILTRON',
                'shortDescription': 'Oil filter',
            }, price_net=3.05,
        )

    def _make_po(self):
        return self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'product_qty': 2,
                'price_unit': 3.05,
            })],
        })

    def test_is_ic_dropship_flag(self):
        po = self._make_po()
        self.assertTrue(po.is_ic_dropship)
        other_vendor = self.env['res.partner'].create({
            'name': 'Some Other Vendor', 'supplier_rank': 1,
        })
        po2 = self.env['purchase.order'].create({
            'partner_id': other_vendor.id,
        })
        self.assertFalse(po2.is_ic_dropship)

    def test_confirm_submits_and_confirms_requisition(self):
        po = self._make_po()
        client = self._mock_client()
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            po.button_confirm()
        self.assertEqual(po.ic_requisition_id, 'RQ-1')
        self.assertEqual(po.ic_requisition_uuid, 'UUID-1')
        self.assertEqual(po.ic_requisition_status, 'CONFIRMED')
        # Line schema: requiredQuantity / unitPriceNet / unitPriceGross.
        _, kwargs = client.submit_requisition.call_args
        line = kwargs['lines'][0]
        self.assertEqual(line['sku'], 'ADDFFF')
        self.assertEqual(line['requiredQuantity'], 2.0)
        self.assertEqual(line['unitPriceNet'], 3.05)
        self.assertGreater(line['unitPriceGross'], line['unitPriceNet'])
        self.assertNotIn('quantity', line)
        client.confirm_requisition.assert_called_once_with(
            'UUID-1', ship_to='F17',
        )

    def test_confirm_blocked_when_ordering_not_allowed(self):
        po = self._make_po()
        client = self._mock_client(get_finances={
            'orderingAllowed': False,
            'overdueBalance': 123.45, 'currencyCode': 'EUR',
        })
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            with self.assertRaises(UserError):
                po.button_confirm()
        client.submit_requisition.assert_not_called()

    def test_confirm_raises_when_not_accepted(self):
        po = self._make_po()
        client = self._mock_client(submit_requisition=[{
            'id': 'UUID-2', 'requisitionId': 'RQ-2',
            'phaseCode': 'REJECTED', 'statusCode': 'ERR',
        }])
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            with self.assertRaises(UserError):
                po.button_confirm()
        client.confirm_requisition.assert_not_called()

    def test_gross_uses_line_tax_when_present(self):
        tax = self.env['account.tax'].create({
            'name': 'Test VAT 19', 'amount': 19.0,
            'amount_type': 'percent', 'type_tax_use': 'purchase',
        })
        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_qty': 1,
                'price_unit': 100.0, 'tax_ids': [(6, 0, [tax.id])],
            })],
        })
        client = self._mock_client()
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            po.button_confirm()
        line = client.submit_requisition.call_args.kwargs['lines'][0]
        self.assertAlmostEqual(line['unitPriceGross'], 119.0, places=2)

    def test_gross_falls_back_to_backend_vat(self):
        self.backend.vat_rate_pct = 20.0
        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_qty': 1,
                'price_unit': 100.0, 'tax_ids': [(5,)],
            })],
        })
        client = self._mock_client()
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            po.button_confirm()
        line = client.submit_requisition.call_args.kwargs['lines'][0]
        self.assertAlmostEqual(line['unitPriceGross'], 120.0, places=2)

    def test_submit_uses_live_ic_price_and_syncs_po_line(self):
        """A stale PO price must not reach IC — the submit flow asks IC
        for the current price, sends IC's numbers, and corrects the PO
        line (this is the ICF299 class of failure)."""
        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_qty': 1,
                'price_unit': 2.16,   # wrong: the SALE price
            })],
        })
        client = self._mock_client(get_price={'lines': [{
            'sku': 'ADDFFF',
            'price': {'customerPriceNet': 1.73,
                      'customerPriceGross': 2.06,
                      'currencyCode': 'EUR'},
        }]})
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            po.button_confirm()
        line = client.submit_requisition.call_args.kwargs['lines'][0]
        self.assertEqual(line['unitPriceNet'], 1.73,
                         "IC's live net price must be sent")
        self.assertEqual(line['unitPriceGross'], 2.06,
                         "IC's live gross price must be sent")
        self.assertEqual(po.order_line.price_unit, 1.73,
                         "PO line synced to the real cost")

    def test_submit_falls_back_to_po_price_without_live_quote(self):
        po = self._make_po()
        client = self._mock_client()  # get_price returns no lines
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            po.button_confirm()
        line = client.submit_requisition.call_args.kwargs['lines'][0]
        self.assertEqual(line['unitPriceNet'], 3.05,
                         "PO price used when IC has no quote")

    @mute_logger(_PO_LOGGER)
    def test_icf299_translated_to_operator_message(self):
        po = self._make_po()
        client = self._mock_client()
        client.submit_requisition.side_effect = UserError(
            'Inter Cars error (400): [{"code":"ICF299",'
            '"details":"Wystapil nieznany blad.",'
            '"errorId":"00b23d57"}]')
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            with self.assertRaises(UserError) as ctx:
                po.button_confirm()
        msg = str(ctx.exception)
        self.assertIn('ICF299', msg)
        self.assertIn('Payment Method', msg,
                      "message must suggest the config knobs to try")
        self.assertIn('00b23d57', msg, "errorId preserved for IC support")

    def test_confirm_blocked_when_no_availability(self):
        """ICF230 prevention: a PO whose SKUs have zero IC availability
        must be blocked before anything is sent."""
        po = self._make_po()
        client = self._mock_client(get_stock=[
            {'sku': 'ADDFFF', 'availability': 0},
        ])
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            with self.assertRaises(UserError) as ctx:
                po.button_confirm()
            self.assertIn('ADDFFF', str(ctx.exception))
        client.submit_requisition.assert_not_called()

    @mute_logger(_PO_LOGGER)
    def test_availability_preflight_soft_fails_on_transport_error(self):
        """A broken stock endpoint must not block ordering — IC's own
        submit validation is the authority."""
        po = self._make_po()
        client = self._mock_client()
        client.get_stock.side_effect = Exception("boom")
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            po.button_confirm()
        client.submit_requisition.assert_called_once()
        self.assertEqual(po.ic_requisition_id, 'RQ-1')

    @mute_logger(_PO_LOGGER)
    def test_icf230_translated_to_operator_message(self):
        po = self._make_po()
        client = self._mock_client()
        client.submit_requisition.side_effect = UserError(
            'Inter Cars /ic/sales/requisition error (400): '
            '[{"code":"ICF230","details":"All SKUs provided in the '
            'request are invalid."}]')
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            with self.assertRaises(UserError) as ctx:
                po.button_confirm()
        msg = str(ctx.exception)
        self.assertIn('ADDFFF', msg, "message must name the SKUs sent")
        self.assertIn('availability', msg)

    @mute_logger(_PO_LOGGER)
    def test_icf209_translated_to_operator_message(self):
        po = self._make_po()
        client = self._mock_client()
        client.submit_requisition.side_effect = UserError(
            'Inter Cars error (400): [{"code":"ICF209",'
            '"details":"DeliveryMethod is not valid"}]')
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            with self.assertRaises(UserError) as ctx:
                po.button_confirm()
        self.assertIn('Default Delivery Method', str(ctx.exception))

    @mute_logger(_PRODUCT_LOGGER)
    def test_live_availability_helper(self):
        client = self._mock_client(get_stock=[
            {'sku': 'X1', 'availability': 4, 'location': 'KOM'},
            {'sku': 'X1', 'availability': 6, 'location': 'F11'},
            {'sku': 'OTHER', 'availability': 99},
        ])
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            avail = self.env['product.product']._baf_ic_live_availability(
                self.backend, 'X1')
        self.assertEqual(avail, 10, "sums locations, ignores other SKUs")
        client.get_stock.side_effect = Exception("net down")
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            self.assertIsNone(
                self.env['product.product']._baf_ic_live_availability(
                    self.backend, 'X1'),
                "unknown availability must be None, not 0")

    def test_confirm_refuses_empty_po(self):
        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
        })
        client = self._mock_client()
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            with self.assertRaises(UserError):
                po.button_confirm()
        client.submit_requisition.assert_not_called()

    def test_confirm_skips_non_ic_products(self):
        plain = self.env['product.product'].create({
            'name': 'Plain part', 'type': 'consu',
        })
        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'order_line': [(0, 0, {
                'product_id': plain.id, 'product_qty': 1,
                'price_unit': 5.0,
            })],
        })
        client = self._mock_client()
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            with self.assertRaises(UserError):
                # Product without ic_sku cannot be sent to IC.
                po.button_confirm()


@tagged('post_install', '-at_install')
class TestBackendModel(IcCase):

    def test_get_default_returns_active_backend(self):
        found = self.env['ic.backend']._get_default()
        self.assertEqual(found, self.backend)

    def test_get_client_requires_token_url_at_auth(self):
        self.backend.token_url = False
        client = self.backend.get_client()
        with self.assertRaises(UserError):
            client._auth()

    def test_fetch_account_info_snapshots_customer_and_finances(self):
        client = self._mock_client()
        client.get_customer.return_value = {
            'name': 'BAF HANDELS GMBH', 'status': 'ACTIVE',
            'defaultPaymentMethod': '14',
            'defaultDeliveryMethod': 'DIST',
            'paymentMethods': ['14', '20'],
        }
        client.get_finances.return_value = {
            'orderingAllowed': True, 'currencyCode': 'EUR',
        }
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            action = self.backend.action_fetch_account_info()
        self.assertEqual(action['params']['type'], 'success')
        self.assertIn('DIST', action['params']['message'])
        self.assertTrue(self.backend.account_info_date)
        snapshot = json.loads(self.backend.account_info)
        self.assertEqual(snapshot['customer']['status'], 'ACTIVE')
        self.assertTrue(snapshot['finances']['orderingAllowed'])

    def test_fetch_account_info_survives_finances_denial(self):
        client = self._mock_client()
        client.get_customer.return_value = {'status': 'ACTIVE',
                                            'paymentMethods': []}
        client.get_finances.side_effect = UserError("403 forbidden")
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            self.backend.action_fetch_account_info()
        snapshot = json.loads(self.backend.account_info)
        self.assertEqual(snapshot['finances'], {},
                         "partial snapshot beats none")


@tagged('post_install', '-at_install')
class TestPriceRefreshCron(IcCase):
    """Daily cron that re-quotes materialised products and updates
    their cost + sale price in place."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.product = cls.env['product.product']._baf_find_or_create_ic(
            cls.backend, {
                'sku': 'ADDFFF', 'brand': 'FILTRON',
                'shortDescription': 'Oil filter',
            }, price_net=3.05,
        )
        # Distinct SKU + no EAN so the barcode uniqueness constraint
        # doesn't collide with the first fixture.
        cls.other = cls.env['product.product']._baf_find_or_create_ic(
            cls.backend, {
                'sku': 'BEF134', 'brand': 'VALEO',
                'shortDescription': 'Radiator', 'eans': [],
            }, price_net=10.00,
        )

    def _supplier_price(self, product):
        return self.env['product.supplierinfo'].search([
            ('product_tmpl_id', '=', product.product_tmpl_id.id),
            ('partner_id', '=', self.vendor.id),
        ], limit=1).price

    def test_refresh_updates_cost_and_sale_price(self):
        client = self._mock_client(get_price={'lines': [
            {'sku': 'ADDFFF',
             'price': {'customerPriceNet': 4.00, 'currencyCode': 'EUR'}},
            {'sku': 'BEF134',
             'price': {'customerPriceNet': 12.50, 'currencyCode': 'EUR'}},
        ]})
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            stats = self.backend._baf_refresh_prices()
        self.assertEqual(stats['quoted'], 2)
        self.assertEqual(stats['declined'], 0)
        self.assertEqual(self._supplier_price(self.product), 4.00)
        self.assertEqual(self._supplier_price(self.other), 12.50)
        self.assertAlmostEqual(
            self.product.product_tmpl_id.list_price,
            round(4.00 * 1.25, 2), places=2,
            msg="sale price re-derived from the new cost")

    def test_refresh_leaves_declined_skus_untouched(self):
        original_cost = self._supplier_price(self.product)
        original_list = self.product.product_tmpl_id.list_price
        client = self._mock_client(get_price={'lines': [
            {'sku': 'BEF134',
             'price': {'customerPriceNet': 12.50, 'currencyCode': 'EUR'}},
        ]})  # ADDFFF deliberately unquoted
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            stats = self.backend._baf_refresh_prices()
        self.assertEqual(stats['quoted'], 1)
        self.assertEqual(stats['declined'], 1)
        self.assertEqual(self._supplier_price(self.product), original_cost,
                         "unquoted cost must not change")
        self.assertEqual(self.product.product_tmpl_id.list_price,
                         original_list,
                         "unquoted sale price must not change")

    def test_refresh_survives_transport_error(self):
        original_cost = self._supplier_price(self.product)
        client = self._mock_client()
        client.get_price.side_effect = Exception("boom")
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            stats = self.backend._baf_refresh_prices()
        self.assertEqual(stats['quoted'], 0)
        self.assertEqual(stats['declined'], 2)
        self.assertEqual(self._supplier_price(self.product), original_cost,
                         "a broken chunk must never wipe prices")

    def test_refresh_skips_backends_without_matching_products(self):
        other_vendor = self.env['res.partner'].create({
            'name': 'Empty IC Vendor', 'supplier_rank': 1,
        })
        empty = self.env['ic.backend'].create({
            'name': 'IC Empty', 'vendor_id': other_vendor.id,
            'client_id': 'x', 'client_secret': 'y',
            'token_url': 'https://token.invalid/oauth2/token',
            'currency_id': self.env.ref('base.EUR').id,
            'ship_to': 'F17', 'market': 'de',
        })
        with patch.object(type(empty), 'get_client') as mocked:
            stats = empty._baf_refresh_prices()
        self.assertEqual(
            stats, {'quoted': 0, 'declined': 0, 'batches': 0},
            "no products for the backend's vendor → nothing to quote")
        mocked.assert_not_called()

    def test_cron_wrapper_calls_backend_refresh(self):
        client = self._mock_client(get_price={'lines': [
            {'sku': 'ADDFFF',
             'price': {'customerPriceNet': 2.00, 'currencyCode': 'EUR'}},
            {'sku': 'BEF134',
             'price': {'customerPriceNet': 8.00, 'currencyCode': 'EUR'}},
        ]})
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            self.env['ic.backend']._cron_refresh_prices()
        self.assertEqual(self._supplier_price(self.product), 2.00)


@tagged('post_install', '-at_install')
class TestCatalogRefreshCron(IcCase):
    """Daily cron that pulls today's ProductInformation CSV and
    replaces the cache."""

    _MINI_CSV = (
        b"TOW_KOD;IC_INDEX;TEC_DOC;TEC_DOC_PROD;ARTICLE_NUMBER;MANUFACTURER;"
        b"SHORT_DESCRIPTION;DESCRIPTION;BARCODES;PACKAGE_WEIGHT;PACKAGE_LENGTH;"
        b"PACKAGE_WIDTH;PACKAGE_HEIGHT;CUSTOM_CODE;BLOCKED_RETURN\n"
        b"CRON1;IDX 1;IDX1;1;IDX1;FILTRON;Sample;Sample descr;"
        b";0,1;1;1;1;12345678;false\n"
    )

    def test_cron_replaces_cache_from_downloaded_csv(self):
        self.backend.write({'csv_login': 'F04217',
                            'csv_password': 'secret'})
        # Seed a row that must be gone after the refresh.
        self.env['ic.product.info'].create({'tow_kod': 'OLDROW'})
        with patch.object(type(self.backend),
                          '_baf_fetch_product_info_csv',
                          return_value=self._MINI_CSV):
            self.env['ic.backend']._cron_refresh_catalog_csv()
        rows = self.env['ic.product.info'].search([]).mapped('tow_kod')
        self.assertEqual(rows, ['CRON1'],
                         "the cron must replace the snapshot, not append")

    @mute_logger('odoo.addons.ic_intercars.models.ic_backend')
    def test_cron_soft_fails_per_backend(self):
        self.backend.write({'csv_login': 'F04217',
                            'csv_password': 'secret'})
        with patch.object(type(self.backend),
                          '_baf_fetch_product_info_csv',
                          side_effect=UserError("IC HTTP 500")):
            # Must NOT raise — the cron logs and moves on so the next
            # scheduled run isn't cancelled.
            self.env['ic.backend']._cron_refresh_catalog_csv()

    def test_cron_ignores_backends_without_csv_credentials(self):
        # Fixture backend has no csv_login/password.
        with patch.object(type(self.backend),
                          '_baf_fetch_product_info_csv') as mocked:
            self.env['ic.backend']._cron_refresh_catalog_csv()
        mocked.assert_not_called()

    def test_unzip_helper_returns_csv_from_zip(self):
        import io as _io
        import zipfile as _zf
        buf = _io.BytesIO()
        with _zf.ZipFile(buf, 'w') as z:
            z.writestr('inner.csv', b'HEADER\nrow\n')
        result = self.backend._baf_unzip_csv_if_needed(
            buf.getvalue(), 'ProductInformation_2026-08-01.csv.zip')
        self.assertEqual(result, b'HEADER\nrow\n')

    def test_unzip_helper_returns_raw_when_not_zip(self):
        raw = b"TOW_KOD;IC_INDEX\nA;B\n"
        self.assertEqual(
            self.backend._baf_unzip_csv_if_needed(raw, 'file.csv'),
            raw)
