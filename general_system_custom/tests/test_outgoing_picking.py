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
