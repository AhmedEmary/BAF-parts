from odoo.tests.common import TransactionCase
from odoo import Command

class TestStockMovePallet(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super(TestStockMovePallet, cls).setUpClass()
        # Disable tracking to speed up tests
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))

        # 1. Create base records
        cls.partner = cls.env['res.partner'].create({
            'name': 'Test Customer'
        })

        cls.product = cls.env['product.product'].create({
            'name': 'Test Product',
            'type': 'consu',         # Defines it as a physical good
            'is_storable': True,     # Makes it trackable in stock
        })

        # 2. Setup warehouse and stock locations
        cls.warehouse = cls.env['stock.warehouse'].search([('company_id', '=', cls.env.company.id)], limit=1)
        cls.stock_location = cls.warehouse.lot_stock_id

        # Add 100 units to stock so reservation can succeed
        cls.env['stock.quant']._update_available_quantity(cls.product, cls.stock_location, 100)

        # 3. Create a Sale Order to get a valid sale_line_id and picking flow
        cls.sale_order = cls.env['sale.order'].create({
            'partner_id': cls.partner.id,
            'order_line': [
                Command.create({
                    'product_id': cls.product.id,
                    'product_uom_qty': 10.0,
                    'reserve_qty': True, # Custom field from sales_order_line
                })
            ]
        })
        cls.sale_order.action_confirm()

        # Get the generated picking and move
        cls.picking = cls.sale_order.picking_ids[0]
        cls.move = cls.picking.move_ids[0]

        # Prepare custom reserved qty for test
        cls.move.reserved_qty_custom = 5.0

        # 4. Create a Warehouse Pallet
        cls.pallet = cls.env['warehouse.pallet'].create({
            'partner_id': cls.partner.id,
            'state': 'open'
        })

    def test_01_stock_move_pallet_info_sync(self):
        """Test the synchronization (Create, Write, Unlink) between Pallet Info and Warehouse Checking Line."""

        # --- TEST CREATE ---
        pallet_info = self.env['stock.move.pallet.info'].create({
            'move_id': self.move.id,
            'pallet_id': self.pallet.id,
            'quantity': 3.0,
        })

        # Verify checking line was created
        domain = [
            ('pallet_id', '=', self.pallet.id),
            ('product_id', '=', self.product.id),
            ('delivery_picking_id', '=', self.picking.id)
        ]
        checking_line = self.env['warehouse.checking.line'].search(domain)
        self.assertTrue(checking_line, "Warehouse checking line should be auto-created.")
        self.assertEqual(checking_line.fulfill_qty, 3.0, "Quantity should be synced on create.")

        # --- TEST WRITE (Update qty) ---
        pallet_info.write({'quantity': 5.0})
        self.assertEqual(checking_line.fulfill_qty, 5.0, "Warehouse checking line should update its qty on write.")

        # --- TEST WRITE (Qty to zero) ---
        pallet_info.write({'quantity': 0.0})
        self.assertFalse(checking_line.exists(), "Warehouse checking line should be unlinked when qty drops to 0.")

        # Bring it back for the unlink test
        pallet_info.write({'quantity': 2.0})
        checking_line = self.env['warehouse.checking.line'].search(domain)
        self.assertTrue(checking_line.exists(), "Line should be recreated when qty is restored.")

        # --- TEST UNLINK ---
        pallet_info.unlink()
        self.assertFalse(checking_line.exists(), "Warehouse checking line should be deleted if the pallet info is deleted.")

    def test_02_compute_qty_from_stock(self):
        """Test the computation of Pick from Stock field."""

        # We manually bypass the system to test the compute directly
        self.move.write({
            'quantity': 10.0, # Testing based on standard quantity field logic
            'qty_scanned': 4.0
        })

        # Trigger depends
        self.move._compute_qty_from_stock()

        # Expected: max(0, 10 - 4) = 6.0
        self.assertEqual(self.move.qty_from_stock, 6.0, "_compute_qty_from_stock calculated incorrectly.")

        # Test negative bound
        self.move.qty_scanned = 12.0
        self.move._compute_qty_from_stock()
        # Expected: max(0, 10 - 12) = 0.0
        self.assertEqual(self.move.qty_from_stock, 0.0, "_compute_qty_from_stock should floor at 0.0.")

    def test_03_action_view_pallet_infos(self):
        """Test the window action structure for opening Pallet Infos."""
        action = self.move.action_view_pallet_infos()

        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], 'stock.move.pallet.info')
        self.assertIn(('move_id', '=', self.move.id), action['domain'])

    def test_04_action_assign_override(self):
        """Test the performance optimized raw-SQL custom reservation logic."""

        # Record the original demand
        original_uom_qty = self.move.product_uom_qty # Should be 10.0 from SO
        self.assertEqual(original_uom_qty, 10.0)

        # Unreserve any standard Odoo reservations that happened upon SO Confirm
        self.move._do_unreserve()

        # Run our custom action assign
        # In setup, we set reserved_qty_custom = 5.0 and reserve_qty = True
        self.move._action_assign()

        # 1. Verify the Original Qty was restored safely via SQL
        self.assertEqual(
            self.move.product_uom_qty,
            original_uom_qty,
            "The product_uom_qty should be perfectly restored after the raw SQL operation."
        )

        # 2. Verify that Odoo only reserved the 'custom' amount (5.0), not the full 10.0
        # Check against Odoo 16/17 move_line logic
        actual_reserved = sum(self.move.move_line_ids.mapped('quantity'))

        self.assertEqual(
            actual_reserved,
            5.0,
            "The core _action_assign should have reserved exactly the reserved_qty_custom amount."
        )
