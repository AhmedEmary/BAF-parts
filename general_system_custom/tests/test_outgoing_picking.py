from odoo.tests import TransactionCase, tagged
from odoo import Command

@tagged('post_install', '-at_install')
class TestOutgoingPalletInfo(TransactionCase):

    def setUp(self):
        super().setUp()
        # Setup Master Data
        self.customer = self.env['res.partner'].create({'name': 'Customer Test'})
        self.vendor = self.env['res.partner'].create({'name': 'Vendor Test'})
        
        # Use consu + is_storable=True for compatibility with Odoo 18/19
        self.product = self.env['product.product'].create({
            'name': 'Test Product',
            'type': 'consu',
            'is_storable': True, 
        })

    def test_reservation_and_multiple_sessions(self):
        """ Test scenario: Initial stock reservation + 2 different scan sessions. """
        # 1. Setup Stock (2 units available)
        stock_loc = self.env.ref('stock.stock_location_stock')
        self.env['stock.quant'].create({
            'product_id': self.product.id,
            'location_id': stock_loc.id,
            'quantity': 2.0,
        })

        # 2. Create SO (Demand 10)
        self.so = self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'order_line': [Command.create({
                'product_id': self.product.id,
                'product_uom_qty': 10.0,
                'price_unit': 100.0,
            })]
        })
        
        # Reserve the 2 units from local stock
        line = self.so.order_line[0]
        line._compute_stock_quantity() 
        line.reserve_qty = True
        line.reserved_qty = 2.0 
        
        self.so.action_confirm() 
        self.delivery = self.so.picking_ids
        move = self.delivery.move_ids[0]
        
        # 3. Create PO for the full amount (Supply)
        self.po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'sale_order_id': self.so.id,
            'order_line': [Command.create({
                'product_id': self.product.id,
                'product_qty': 10.0, 
                'price_unit': 50.0,
            })]
        })
        self.po.button_confirm()
        # Initialize custom open qty field for search logic
        self.po.order_line.qty_split = 10.0

        # 4. Session 1: Receive 3 units on Pallet A
        pallet_a = self.env['warehouse.pallet'].create({'partner_id': self.customer.id, 'name': 'PLT-A'})
        session1 = self.env['warehouse.checking.session'].create({'partner_id': self.vendor.id})
        
        self.env['warehouse.checking.line'].create({
            'session_id': session1.id,
            'product_id': self.product.id,
            'purchase_order_id': self.po.id,
            'purchase_line_id': self.po.order_line[0].id,
            'sale_order_id': self.so.id,
            'sale_line_id': self.so.order_line[0].id,
            'delivery_picking_id': self.delivery.id,
            'fulfill_qty': 3.0,
            'open_qty': 10.0,
            'pallet_id': pallet_a.id
        })
        session1.action_validate()

        # 5. Session 2: Receive 4 units on Pallet B
        pallet_b = self.env['warehouse.pallet'].create({'partner_id': self.customer.id, 'name': 'PLT-B'})
        session2 = self.env['warehouse.checking.session'].create({'partner_id': self.vendor.id})
        
        self.env['warehouse.checking.line'].create({
            'session_id': session2.id,
            'product_id': self.product.id,
            'purchase_order_id': self.po.id,
            'purchase_line_id': self.po.order_line[0].id,
            'sale_order_id': self.so.id,
            'sale_line_id': self.so.order_line[0].id,
            'delivery_picking_id': self.delivery.id,
            'fulfill_qty': 4.0,
            'open_qty': 7.0, 
            'pallet_id': pallet_b.id
        })
        session2.action_validate()

        # 6. Verify Results
        move.invalidate_recordset()
        self.assertEqual(move.qty_scanned, 7.0, "Total Scanned should be 7")
        self.assertEqual(move.qty_from_stock, 2.0)
        self.assertEqual(len(move.pallet_info_ids), 2)

    def test_reset_draft_clears_info(self):
        """ Test that resetting the session correctly reverts the Delivery Order info. """
        self.so = self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'order_line': [Command.create({
                'product_id': self.product.id,
                'product_uom_qty': 10.0,
            })]
        })
        self.so.action_confirm() 
        self.delivery = self.so.picking_ids
        
        self.po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'sale_order_id': self.so.id,
            'order_line': [Command.create({
                'product_id': self.product.id,
                'product_qty': 10.0,
            })]
        })
        self.po.button_confirm()
        self.po.order_line.qty_split = 10.0
        
        pallet = self.env['warehouse.pallet'].create({'partner_id': self.customer.id, 'name': 'RESET-PLT'})
        session = self.env['warehouse.checking.session'].create({'partner_id': self.vendor.id})
        
        # Ensure exact mapping for validation to avoid "Zero Quantity" error
        self.env['warehouse.checking.line'].create({
            'session_id': session.id,
            'product_id': self.product.id,
            'purchase_order_id': self.po.id,
            'purchase_line_id': self.po.order_line[0].id, # MANDATORY for _update_picking_moves
            'sale_order_id': self.so.id,
            'sale_line_id': self.so.order_line[0].id,
            'delivery_picking_id': self.delivery.id,
            'fulfill_qty': 4.0,
            'open_qty': 10.0,
            'pallet_id': pallet.id
        })

        session.action_validate()
        move = self.delivery.move_ids[0]
        self.assertEqual(move.qty_scanned, 4.0)
        self.assertTrue(move.pallet_info_ids)

        session.action_reset_draft()

        self.assertEqual(move.qty_scanned, 0.0, "Qty Scanned should revert to 0")
        self.assertFalse(any(p.quantity > 0 for p in move.pallet_info_ids), "Pallet info qty should be 0 or unlinked")
