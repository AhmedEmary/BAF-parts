from odoo.tests import TransactionCase, tagged, Form
from odoo.exceptions import UserError
from odoo import Command

@tagged('post_install', '-at_install')
class TestIntelliwiseFlows(TransactionCase):

    def setUp(self):
        super(TestIntelliwiseFlows, self).setUp()
        
        # 1. Setup Basic Data: Brand, Partner, Product
        self.brand = self.env['product.brand'].create({
            'name': 'Test Brand',
            'description': 'A test brand'
        })
        
        self.vendor = self.env['res.partner'].create({'name': 'Test Vendor'})
        self.customer = self.env['res.partner'].create({'name': 'Test Customer'})
        
        self.product = self.env['product.product'].create({
            'name': 'Test Product',
            'type': 'consu',
            'is_storable': True,
            'brand': self.brand.id,
            'standard_price': 50.0,
            'list_price': 100.0,
            'seller_ids': [Command.create({'partner_id': self.vendor.id, 'price': 40.0})],
        })

        # 2. Setup Stock Environment
        self.stock_location = self.env.ref('stock.stock_location_stock')
        self.env['stock.quant'].create({
            'product_id': self.product.id,
            'location_id': self.stock_location.id,
            'quantity': 10.0, # Start with 10 units in stock
        })

    def test_stock_reservation_logic(self):
        """ Test that stock reservation works and respects availability """
        so = self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'order_line': [Command.create({
                'product_id': self.product.id,
                'product_uom_qty': 20.0,
                'price_unit': 100.0,
            })]
        })
        line = so.order_line[0]

        self.assertEqual(line.stock_quantity, 10.0, "Stock Available should be 10")
        self.assertEqual(line.qty_to_purchase, 20.0)

        so.update_all_sale_line_reserved_qty()

        self.assertTrue(line.reserve_qty)
        self.assertEqual(line.reserved_qty, 10.0)
        self.assertEqual(line.qty_to_purchase, 10.0)
        self.assertEqual(so.coverage_percentage, 50.0)

    def test_create_purchase_order_flow(self):
        """ Test creation of PO from SO for missing quantities """
        so = self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'order_line': [Command.create({
                'product_id': self.product.id,
                'product_uom_qty': 15.0,
                'reserved_qty': 0.0,
                'reserve_qty': False,
            })]
        })
        line = so.order_line[0]
        self.assertEqual(line.qty_to_purchase, 15.0)

        so.action_create_purchase_order()
        
        self.assertTrue(so.purchase_ids)
        po = so.purchase_ids[0]
        self.assertEqual(po.partner_id, self.vendor)
        self.assertEqual(len(po.order_line), 1)
        self.assertEqual(po.order_line.product_qty, 15.0)

    def test_purchased_qty_calculation(self):
        """ Test that purchased_qty correctly sums up quantities from linked POs """
        so = self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'order_line': [Command.create({'product_id': self.product.id, 'product_uom_qty': 50.0})]
        })
        line = so.order_line[0]

        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'sale_order_id': so.id,
            'order_line': [Command.create({'product_id': self.product.id, 'product_qty': 20.0, 'price_unit': 40.0})]
        })
        po.button_confirm()
        
        line.invalidate_recordset()
        self.assertEqual(line.purchased_qty, 20.0)

        picking = po.picking_ids
        for move in picking.move_ids:
            move.quantity = 5.0
        res = picking.button_validate()
        
        if isinstance(res, dict) and res.get('res_model') == 'stock.backorder.confirmation':
            wizard = self.env['stock.backorder.confirmation'].with_context(res['context']).create({
                'pick_ids': [Command.set(picking.ids)]
            })
            wizard.process()

        po.order_line.invalidate_recordset() 
        line.invalidate_recordset()
        self.assertEqual(line.purchased_qty, 15.0)

    def test_qty_to_purchase_calculation(self):
        """ Test math: qty_to_purchase = Demand - Reserved - Purchased """
        so = self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'order_line': [Command.create({
                'product_id': self.product.id,
                'product_uom_qty': 100.0,
                'reserve_qty': True,
                'reserved_qty': 10.0,
            })]
        })

        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'sale_order_id': so.id,
            'order_line': [Command.create({'product_id': self.product.id, 'product_qty': 30.0, 'price_unit': 40.0})]
        })
        po.button_confirm()
        
        line = so.order_line[0]
        line._compute_qty_to_purchase()
        self.assertEqual(line.qty_to_purchase, 60.0)

        line.reserve_qty = False
        line._compute_qty_to_purchase()
        self.assertEqual(line.qty_to_purchase, 70.0)
