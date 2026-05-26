from unittest.mock import patch, MagicMock

from odoo.tests import TransactionCase, tagged
from odoo.exceptions import UserError


@tagged('post_install', '-at_install')
class TestShippingCustomLogic(TransactionCase):

    def setUp(self):
        super().setUp()

        company = self.env.company

        # Company partner needs a full address — the FedEx pre-flight
        # validator requires country/street/city/zip on both ends.
        company.partner_id.write({
            'country_id': self.env.ref('base.it').id,
            'street': company.partner_id.street or 'Via Italia 10',
            'city': company.partner_id.city or 'Milan',
            'zip': company.partner_id.zip or '20121',
        })

        self.partner = self.env['res.partner'].create({
            'name': 'Test Customer',
            'country_id': self.env.ref('base.it').id,
            'street': 'Via Roma 1',
            'city': 'Milan',
            'zip': '20100',
        })

        self.product = self.env['product.product'].create({
            'name': 'Test Product',
            'type': 'consu',
            'weight': 5.0,
            'list_price': 100.0,
        })

        self.fedex_account = self.env['shipping.provider.account'].create({
            'name': 'Test FedEx Account',
            'provider': 'fedex',
            'fedex_client_id': 'dummy_key',
            'fedex_client_secret': 'dummy_secret',
            'account_number': '123456789',
            'prod_environment': False,
        })

        # Standard Odoo pallet packaging type used as the source of dims.
        self.pallet_type = self.env['stock.package.type'].create({
            'name': 'Test EUR Pallet',
            'packaging_length': 120.0,
            'width': 80.0,
            'height': 100.0,
            'base_weight': 25.0,
        })

        # Build an outgoing transfer for the partner.
        picking_type = self.env['stock.picking.type'].search([
            ('code', '=', 'outgoing'),
            ('company_id', '=', company.id),
        ], limit=1)
        self.picking = self.env['stock.picking'].create({
            'partner_id': self.partner.id,
            'picking_type_id': picking_type.id,
            'location_id': picking_type.default_location_src_id.id,
            'location_dest_id': self.partner.property_stock_customer.id,
            'move_ids': [(0, 0, {
                'product_id': self.product.id,
                'product_uom_qty': 2.0,
                'product_uom': self.product.uom_id.id,
                'location_id': picking_type.default_location_src_id.id,
                'location_dest_id': self.partner.property_stock_customer.id,
            })],
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _make_fedex_client(self):
        from odoo.addons.shipping_custom.models.fedex_client_api import (
            FedexAPIClient,
        )
        self.fedex_account.fedex_rest_access_token = 'token'
        client = FedexAPIClient(self.fedex_account)
        # Skip the address-resolve network call.
        client.validate_address = lambda partner: None
        return client

    def _good_pallet(self, **overrides):
        vals = {
            'picking_id': self.picking.id,
            'package_type_id': self.pallet_type.id,
            'name': 'PLT-OK',
            'weight': 25.0,
            'length': 120.0,
            'width': 80.0,
            'height': 100.0,
        }
        vals.update(overrides)
        return self.env['shipping.picking.package'].create(vals)

    def _shipper(self):
        return self.env.company.partner_id

    # ------------------------------------------------------------------
    # Account / connection
    # ------------------------------------------------------------------
    @patch('odoo.addons.shipping_custom.models.fedex_client_api.requests.post')
    def test_01_fedex_connection_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.json.return_value = {'access_token': 'fake_test_token_999'}
        mock_post.return_value = mock_response

        result = self.fedex_account.action_test_connection()

        self.assertEqual(
            self.fedex_account.fedex_rest_access_token, 'fake_test_token_999',
        )
        self.assertEqual(result['type'], 'ir.actions.client')
        self.assertEqual(result['params']['type'], 'success')

    # ------------------------------------------------------------------
    # Picking shipping package: dim defaults from stock.package.type
    # ------------------------------------------------------------------
    def test_02_picking_package_onchange_pulls_defaults_from_type(self):
        pkg = self.env['shipping.picking.package'].new({
            'picking_id': self.picking.id,
            'package_type_id': self.pallet_type.id,
        })
        pkg._onchange_package_type_id()
        self.assertEqual(pkg.length, 120.0)
        self.assertEqual(pkg.width, 80.0)
        self.assertEqual(pkg.height, 100.0)
        self.assertEqual(pkg.weight, 25.0)
        self.assertEqual(pkg.name, self.pallet_type.name)

    # ------------------------------------------------------------------
    # Picking fetch rates → populates picking.shipping_option_ids
    # ------------------------------------------------------------------
    @patch(
        'odoo.addons.shipping_custom.models.fedex_client_api.'
        'FedexAPIClient.fetch_all_shipping_rates'
    )
    def test_03_picking_fetch_rates(self, mock_fetch_rates):
        self._good_pallet()
        self.env.company.shipping_account_ids = [
            (6, 0, [self.fedex_account.id]),
        ]

        mock_fetch_rates.return_value = [
            {
                'service_type': 'PRIORITY_OVERNIGHT',
                'delivery_time': 'Tomorrow',
                'cost': 55.0,
                'packaging_type': 'YOUR_PACKAGING',
            },
            {
                'service_type': 'FEDEX_GROUND',
                'delivery_time': '3 Days',
                'cost': 15.0,
                'packaging_type': 'YOUR_PACKAGING',
            },
        ]

        self.picking.action_fetch_picking_shipping_rates()

        options = self.picking.shipping_option_ids
        self.assertEqual(len(options), 2)
        self.assertTrue(
            any(
                name.startswith('[FEDEX] Priority Overnight')
                for name in options.mapped('service_name')
            ),
        )
        self.assertEqual(
            sorted(options.mapped('cost')), [15.0, 55.0],
        )

    def test_04_fetch_rates_without_pallet_raises(self):
        self.env.company.shipping_account_ids = [
            (6, 0, [self.fedex_account.id]),
        ]
        with self.assertRaises(UserError):
            self.picking.action_fetch_picking_shipping_rates()

    def test_05_fetch_rates_without_account_raises(self):
        self._good_pallet()
        self.env.company.shipping_account_ids = [(5, 0, 0)]
        with self.assertRaises(UserError):
            self.picking.action_fetch_picking_shipping_rates()

    # ------------------------------------------------------------------
    # Select rate creates a draft delivery order with packages copied
    # ------------------------------------------------------------------
    def test_06_select_rate_creates_draft_order_with_packages(self):
        pallet = self._good_pallet()
        option = self.env['picking.shipping.option'].create({
            'picking_id': self.picking.id,
            'provider_account_id': self.fedex_account.id,
            'raw_service_type': 'FEDEX_GROUND',
            'raw_packaging_type': 'YOUR_PACKAGING',
            'service_name': '[FEDEX] Fedex Ground',
            'cost': 30.0,
        })

        action = option.action_select_rate()
        self.assertEqual(action['res_model'], 'shipping.delivery.order')
        order = self.env['shipping.delivery.order'].browse(action['res_id'])

        self.assertEqual(order.state, 'draft')
        self.assertEqual(order.picking_id, self.picking)
        self.assertEqual(order.service_type, 'FEDEX_GROUND')
        self.assertEqual(len(order.package_ids), 1)
        pkg = order.package_ids
        self.assertEqual(pkg.package_type_id, self.pallet_type)
        self.assertEqual(pkg.weight, pallet.weight)
        self.assertEqual(pkg.length, pallet.length)
        # Shipper / recipient are pre-filled from the partners.
        self.assertEqual(order.recipient_country_id, self.partner.country_id)
        self.assertEqual(
            order.shipper_country_id, self.env.company.partner_id.country_id,
        )
        # No tracking yet.
        self.assertFalse(self.picking.tracking_number)

    # ------------------------------------------------------------------
    # Generate label posts back to the picking
    # ------------------------------------------------------------------
    @patch(
        'odoo.addons.shipping_custom.models.fedex_client_api.'
        'FedexAPIClient.submit_shipment'
    )
    def test_07_generate_label_posts_back_to_picking(self, mock_submit):
        self._good_pallet()
        option = self.env['picking.shipping.option'].create({
            'picking_id': self.picking.id,
            'provider_account_id': self.fedex_account.id,
            'raw_service_type': 'FEDEX_GROUND',
            'service_name': '[FEDEX] Fedex Ground',
            'cost': 30.0,
        })
        action = option.action_select_rate()
        order = self.env['shipping.delivery.order'].browse(action['res_id'])

        # Minimal valid PDF, so _save_label_attachments can merge it.
        valid_minimal_pdf_b64 = (
            "JVBERi0xLjAKMSAwIG9iajw8L1R5cGUvQ2F0YWxvZy9QYWdlcyAyIDAgUj4+ZW5kb2JqIDIgMC"
            "BvYmo8PC9UeXBlL1BhZ2VzL0tpZHNbMyAwIFJdL0NvdW50IDE+PmVuZG9iaiAzIDAgb2JqPDwv"
            "VHlwZS9QYWdlL01lZGlhQm94WzAgMCAzIDNdL1BhcmVudCAyIDAgUj4+ZW5kb2JqCnhyZWYKMCA"
            "0CjAwMDAwMDAwMDAgNjU1MzUgZgowMDAwMDAwMDEwIDAwMDAwIG4KMDAwMDAwMDA1MyAwMDAw"
            "MCBuCjAwMDAwMDAxMDIgMDAwMDAgbgp0cmFpbGVyPDwvU2l6ZSA0L1Jvb3QgMSAwIFI+PgpzdG"
            "FydHhyZWYKMTQ5CiUlRU9GCg=="
        )
        mock_submit.return_value = {
            'success': True,
            'status_code': 200,
            'tracking_number': '123456789012',
            'price': 30.0,
            'labels': [(
                'FedEx_label_123456789012_1.pdf', valid_minimal_pdf_b64,
            )],
            'response_text': '',
            'error_message': '',
            'request_payload': {},
            'endpoint': '/ship/v1/shipments',
            'method': 'POST',
        }

        order.action_generate_label()

        self.assertEqual(order.state, 'confirmed')
        self.assertEqual(order.tracking_number, '123456789012')
        # Tracking flows back onto the picking.
        self.assertEqual(self.picking.tracking_number, '123456789012')
        self.assertEqual(
            self.picking.selected_shipping_service, '[FEDEX] Fedex Ground',
        )
        self.assertEqual(self.picking.provider_account_id, self.fedex_account)

    # ------------------------------------------------------------------
    # Void shipment → cancels order + clears picking tracking
    # ------------------------------------------------------------------
    @patch(
        'odoo.addons.shipping_custom.models.fedex_client_api.'
        'FedexAPIClient.void_shipment'
    )
    def test_08_void_shipment(self, mock_void):
        order = self.env['shipping.delivery.order'].create({
            'picking_id': self.picking.id,
            'provider_account_id': self.fedex_account.id,
            'service_type': 'FEDEX_GROUND',
            'service_name': 'FedEx Ground',
            'tracking_number': '123456789012',
            'state': 'confirmed',
        })
        self.picking.write({
            'tracking_number': '123456789012',
            'selected_shipping_service': 'FedEx Ground',
            'provider_account_id': self.fedex_account.id,
        })
        mock_void.return_value = True

        self.picking.action_cancel_shipment()

        mock_void.assert_called_once_with('123456789012')
        self.assertEqual(order.state, 'cancelled')
        self.assertFalse(self.picking.tracking_number)
        self.assertFalse(self.picking.selected_shipping_service)
        self.assertFalse(self.picking.provider_account_id)

    # ------------------------------------------------------------------
    # FedEx pre-flight: invalid pallets are caught before any call
    # ------------------------------------------------------------------
    def test_09_fedex_rejects_zero_weight_pallet(self):
        client = self._make_fedex_client()
        bad = self._good_pallet(name='PLT-NOWEIGHT', weight=0.0)

        with self.assertRaises(UserError) as cm:
            client.fetch_all_shipping_rates(
                ship_from=self._shipper(),
                ship_to=self.partner,
                pallets=bad,
                currency_code='EUR',
            )
        msg = str(cm.exception)
        self.assertIn('PLT-NOWEIGHT', msg)
        self.assertIn('weight', msg)

    def test_10_fedex_lists_every_bad_pallet(self):
        client = self._make_fedex_client()
        bad_a = self._good_pallet(name='PLT-A', weight=0.0)
        bad_b = self._good_pallet(name='PLT-B', height=0.0)
        ok = self._good_pallet(name='PLT-OK')
        pallets = bad_a | bad_b | ok

        with self.assertRaises(UserError) as cm:
            client.fetch_all_shipping_rates(
                ship_from=self._shipper(),
                ship_to=self.partner,
                pallets=pallets,
                currency_code='EUR',
            )
        msg = str(cm.exception)
        self.assertIn('PLT-A', msg)
        self.assertIn('PLT-B', msg)
        self.assertNotIn('PLT-OK', msg)

    @patch(
        'odoo.addons.shipping_custom.models.fedex_client_api.requests.request'
    )
    def test_11_fedex_request_uses_your_packaging(self, mock_request):
        client = self._make_fedex_client()
        pallet = self._good_pallet()

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = {
            'output': {
                'rateReplyDetails': [{
                    'serviceType': 'FEDEX_GROUND',
                    'ratedShipmentDetails': [{
                        'currency': 'EUR',
                        'totalNetCharge': 30.0,
                    }],
                }],
            },
        }
        mock_request.return_value = ok_resp

        rates = client.fetch_all_shipping_rates(
            ship_from=self._shipper(),
            ship_to=self.partner,
            pallets=pallet,
            currency_code='EUR',
        )

        self.assertEqual(mock_request.call_count, 1)
        sent = mock_request.call_args.kwargs['json']['requestedShipment']
        self.assertEqual(sent['packagingType'], 'YOUR_PACKAGING')
        self.assertEqual(
            sent['requestedPackageLineItems'][0].get('subPackagingType'),
            'PALLET',
        )
        self.assertEqual(len(rates), 1)
        self.assertEqual(rates[0]['cost'], 30.0)

    @patch(
        'odoo.addons.shipping_custom.models.fedex_client_api.requests.request'
    )
    def test_12_fedex_multi_pallet_sends_each_as_package(self, mock_request):
        client = self._make_fedex_client()
        big = self._good_pallet(name='PLT-BIG', weight=80.0, height=140.0)
        small = self._good_pallet(
            name='PLT-SMALL', weight=12.0,
            length=60.0, width=40.0, height=30.0,
        )
        pallets = big | small

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = {
            'output': {'rateReplyDetails': [{
                'serviceType': 'FEDEX_GROUND',
                'ratedShipmentDetails': [{
                    'currency': 'EUR', 'totalNetCharge': 100.0,
                }],
            }]},
        }
        mock_request.return_value = ok_resp

        client.fetch_all_shipping_rates(
            ship_from=self._shipper(),
            ship_to=self.partner,
            pallets=pallets,
            currency_code='EUR',
        )

        self.assertEqual(mock_request.call_count, 1)
        items = (
            mock_request.call_args.kwargs['json']['requestedShipment']
            ['requestedPackageLineItems']
        )
        self.assertEqual(len(items), 2)
        weights = sorted(it['weight']['value'] for it in items)
        self.assertEqual(weights, [12.0, 80.0])
        heights = sorted(it['dimensions']['height'] for it in items)
        self.assertEqual(heights, [30, 140])

    @patch(
        'odoo.addons.shipping_custom.models.fedex_client_api.requests.request'
    )
    def test_13_fedex_customs_value_threads_into_payload(self, mock_request):
        client = self._make_fedex_client()
        pallet = self._good_pallet()

        be_country = self.env['res.country'].search(
            [('code', '=', 'BE')], limit=1,
        )
        if not be_country:
            be_country = self.env['res.country'].create({
                'name': 'Belgium', 'code': 'BE',
            })
        ship_to = self.env['res.partner'].create({
            'name': 'BE Customer',
            'country_id': be_country.id,
            'street': 'Rue 1', 'city': 'Brussels', 'zip': '1000',
        })

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = {
            'output': {'rateReplyDetails': [{
                'serviceType': 'FEDEX_GROUND',
                'ratedShipmentDetails': [{
                    'currency': 'EUR', 'totalNetCharge': 50.0,
                }],
            }]},
        }
        mock_request.return_value = ok_resp

        client.fetch_all_shipping_rates(
            ship_from=self._shipper(),
            ship_to=ship_to,
            pallets=pallet,
            currency_code='EUR',
            customs_value=275.50,
        )

        sent = mock_request.call_args_list[0].kwargs['json']
        commodities = (
            sent['requestedShipment']
            ['customsClearanceDetail']['commodities']
        )
        self.assertEqual(commodities[0]['customsValue']['amount'], 275.50)
        self.assertEqual(commodities[0]['customsValue']['currency'], 'EUR')

    # ------------------------------------------------------------------
    # build_shipment_payload (label-time) uses the order's packages
    # and never strays from YOUR_PACKAGING
    # ------------------------------------------------------------------
    def test_14_label_payload_uses_your_packaging(self):
        self._good_pallet(name='PLT-LABEL')
        option = self.env['picking.shipping.option'].create({
            'picking_id': self.picking.id,
            'provider_account_id': self.fedex_account.id,
            'raw_service_type': 'FEDEX_GROUND',
            'raw_packaging_type': 'YOUR_PACKAGING',
            'service_name': '[FEDEX] Fedex Ground',
            'cost': 30.0,
        })
        action = option.action_select_rate()
        order = self.env['shipping.delivery.order'].browse(action['res_id'])

        client = self._make_fedex_client()
        payload = client.build_shipment_payload(order)
        self.assertEqual(
            payload['requestedShipment']['packagingType'],
            'YOUR_PACKAGING',
        )
        items = payload['requestedShipment']['requestedPackageLineItems']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['subPackagingType'], 'PALLET')
