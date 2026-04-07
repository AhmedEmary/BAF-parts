from odoo.tests import TransactionCase, tagged
from odoo.exceptions import UserError
from odoo import Command


@tagged('post_install', '-at_install')
class TestWarehouseChecking(TransactionCase):

    def setUp(self):
        super().setUp()
        # 1. Basic Data
        self.vendor = self.env['res.partner'].create({'name': 'Supplier A'})
        self.customer = self.env['res.partner'].create({'name': 'Customer B'})
        
        self.product = self.env['product.product'].create({
            'name': 'Test Item',
            'type': 'consu', # Consumable for easier stock testing
            'is_storable': True, 
            'default_code': 'SKU001',
            'barcode': '123456789'
        })

        # 2. Create Sales Order (Demand)
        self.so = self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'order_line': [Command.create({
                'product_id': self.product.id,
                'product_uom_qty': 10.0,
            })]
        })
        self.so.action_confirm() # Generates Delivery Picking

        # 3. Create Linked Purchase Order (Supply)
        self.po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'sale_order_id': self.so.id, 
            'order_line': [Command.create({
                'product_id': self.product.id,
                'product_qty': 10.0,
                'price_unit': 50.0,
            })]
        })
        self.po.button_confirm() # Generates Incoming Receipt

        self.po.order_line.write({'qty_split': 10.0})

    def _scan_and_add(self, session, sku):
        """ Helper to simulate Scanning + Clicking 'Add Selected' on the Wizard """
        session.scan_sku = sku
        action = session.action_search_sku()
        
        # If the action opens the Wizard, we must 'click' the Add button
        if action and action.get('res_model') == 'warehouse.checking.wizard':
            wizard = self.env['warehouse.checking.wizard'].browse(action['res_id'])
            wizard.line_ids.write({'is_selected': True})
            wizard.action_add_selected()
        
        return action

    def test_scan_and_populate(self):
        """ Test that scanning an SKU finds the correct Open PO/SO lines """
        session = self.env['warehouse.checking.session'].create({
            'partner_id': self.vendor.id
        })

        self._scan_and_add(session, 'SKU001')

        self.assertEqual(len(session.line_ids), 1, "Should find 1 matching line")
        line = session.line_ids[0]
        self.assertEqual(line.sale_order_id, self.so)
        self.assertEqual(line.purchase_order_id, self.po)
        self.assertEqual(line.open_qty, 10.0)
        self.assertEqual(line.fulfill_qty, 10.0, "Fulfill Qty should default to Open Qty")
        self.assertTrue(line.delivery_picking_id, "Delivery Name should be populated from SO Pickings")

    def test_scan_deduplicates_lines(self):
        """ Test that scanning again DOES NOT create duplicates (Logic changed from 'Clear' to 'Edit') """
        session = self.env['warehouse.checking.session'].create({
            'partner_id': self.vendor.id
        })

        # First Scan -> Creates Line
        self._scan_and_add(session, 'SKU001')
        self.assertEqual(len(session.line_ids), 1)

        # Second Scan -> Should find existing line and NOT create a new one (Wizard won't even open for duplicates)
        self._scan_and_add(session, 'SKU001')
        self.assertEqual(len(session.line_ids), 1, "Session should not duplicate lines for the same work")

    def test_fulfill_qty_constraint(self):
        """ Test that user cannot fulfill more than open quantity """
        session = self.env['warehouse.checking.session'].create({
            'partner_id': self.vendor.id
        })
        self._scan_and_add(session, 'SKU001')
        line = session.line_ids[0]

        with self.assertRaises(UserError):
            line.fulfill_qty = 15.0 # Open is 10.0

    def test_validate_process(self):
        """ Test that validating the session updates the Receipt and Reservations """
        session = self.env['warehouse.checking.session'].create({
            'partner_id': self.vendor.id
        })
        
        self._scan_and_add(session, '123456789')
        
        line = session.line_ids[0]
        line.fulfill_qty = 4.0
        pallet = self.env['warehouse.pallet'].create({
            'partner_id': self.customer.id,
            'name': 'PLT-TEST-001'
        })
        line.pallet_id = pallet
        
        session.action_validate()

        picking = self.po.picking_ids
        done_picking = picking.filtered(lambda p: p.state == 'done')
        self.assertTrue(done_picking, "A picking should be validated and Done")
        
        done_move = done_picking.move_ids.filtered(lambda m: m.product_id == self.product)
        self.assertEqual(done_move.quantity, 4.0, "Receipt should have processed 4 units")

        so_line = self.so.order_line[0]
        self.assertEqual(so_line.reserved_qty, 4.0, "SO Line should have reserved the 4 received units")

    def test_complex_reset_flow(self):
        """ 
        Test a full cycle: 
        1. Validate (Full Qty) -> Check PO Received/SO Reserved
        2. Reset -> Check Reversal
        3. Change Qty (Partial) -> Validate -> Check PO Received/SO Reserved (Backorder logic)
        4. Reset -> Check Reversal
        """

        session = self.env['warehouse.checking.session'].create({
            'partner_id': self.vendor.id
        })
        
        self._scan_and_add(session, 'SKU001')
        
        line = session.line_ids[0]
        line.fulfill_qty = 10.0 # Full Quantity
        
        pallet = self.env['warehouse.pallet'].create({
            'partner_id': self.customer.id,
            'name': 'PLT-COMPLEX'
        })
        line.pallet_id = pallet
        
        po_line = self.po.order_line[0]
        so_line = self.so.order_line[0]

        # --- STEP 1: VALIDATE (Full 10.0) ---
        session.action_validate()
        
        # Verify State
        self.assertEqual(session.state, 'done')
        picking = self.po.picking_ids.filtered(lambda p: p.state == 'done')
        self.assertTrue(picking, "Picking should be done")
        
        # Verify Quantities
        po_line.invalidate_recordset()
        so_line.invalidate_recordset()
        self.assertEqual(po_line.qty_received, 10.0, "PO Received Qty should be 10.0")
        self.assertEqual(so_line.reserved_qty, 10.0, "SO Reserved Qty should be 10.0")

        # --- STEP 2: RESET TO DRAFT ---
        session.action_reset_draft()
        
        # Verify State
        self.assertEqual(session.state, 'draft')
        picking = self.po.picking_ids.filtered(lambda p: p.state != 'cancel')
        self.assertEqual(picking.state, 'draft', "Picking should be reset to draft")
        
        # Verify Reversal
        po_line.invalidate_recordset()
        so_line.invalidate_recordset()
        self.assertEqual(po_line.qty_received, 0.0, "PO Received Qty should be reverted to 0.0")
        self.assertEqual(so_line.reserved_qty, 0.0, "SO Reserved Qty should be reverted to 0.0")

        # --- STEP 3: MODIFY & VALIDATE (Partial 6.0) ---
        line.fulfill_qty = 6.0
        session.action_validate()
        
        # Verify State
        self.assertEqual(session.state, 'done')
        
        # In partial validation, standard Odoo creates a backorder. 
        # So we expect one 'done' picking (for 6.0) and one 'assigned' (for 4.0)
        done_picking = self.po.picking_ids.filtered(lambda p: p.state == 'done')
        self.assertTrue(done_picking, "There should be a done picking for the partial amount")
        self.assertEqual(done_picking.move_ids[0].quantity, 6.0)

        po_line.invalidate_recordset()
        so_line.invalidate_recordset()
        self.assertEqual(po_line.qty_received, 6.0, "PO Received Qty should be 6.0")
        self.assertEqual(so_line.reserved_qty, 6.0, "SO Reserved Qty should be 6.0")
        
        session.action_reset_draft()
        self.assertEqual(session.state, 'draft')
        
        po_line.invalidate_recordset()
        so_line.invalidate_recordset()
        self.assertEqual(po_line.qty_received, 0.0, "PO Received Qty should be reverted to 0.0")
        self.assertEqual(so_line.reserved_qty, 0.0, "SO Reserved Qty should be reverted to 0.0")

    def test_pallet_closed_constraint(self):
        """ Test that lines cannot be modified if the assigned Pallet is Closed """
        session = self.env['warehouse.checking.session'].create({
            'partner_id': self.vendor.id
        })
        self._scan_and_add(session, 'SKU001')
        line = session.line_ids[0]
        
        # Create and Assign Pallet
        pallet = self.env['warehouse.pallet'].create({
            'partner_id': self.customer.id,
            'name': 'PLT-LOCKED'
        })
        line.pallet_id = pallet
        
        # Case 1: Pallet is OPEN -> Modification Allowed
        line.fulfill_qty = 5.0
        self.assertEqual(line.fulfill_qty, 5.0)

        # Case 2: Pallet is READY (Closed) -> Modification Forbidden
        pallet.action_mark_ready()
        self.assertEqual(pallet.state, 'ready')
        
        with self.assertRaises(UserError):
            line.write({'fulfill_qty': 8.0})

        # Case 3: Re-open Pallet -> Modification Allowed again
        pallet.action_reopen()
        line.fulfill_qty = 8.0
        self.assertEqual(line.fulfill_qty, 8.0)

    def test_partial_receipt_with_unscanned_items_and_splits(self):
        """
        Test that:
        1. Split lines for the same product are aggregated (e.g. 3 + 4 = 7).
        2. Unscanned items are explicitly set to 0.
        3. A Backorder is created correctly for the remainder.
        """
        # 1. Create a second product (Product B)
        product_b = self.env['product.product'].create({
            'name': 'Product B',
            'type': 'consu',
            'is_storable': True, 
            'default_code': 'SKU002'
        })

        # 2. Create PO with 2 items (10 units each)
        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'order_line': [
                Command.create({'product_id': self.product.id, 'product_qty': 10.0, 'price_unit': 50.0}),
                Command.create({'product_id': product_b.id, 'product_qty': 10.0, 'price_unit': 50.0}),
            ]
        })
        po.button_confirm()
        picking = po.picking_ids[0]

        # 3. Create Session & Pallets
        session = self.env['warehouse.checking.session'].create({'partner_id': self.vendor.id})
        pallet1 = self.env['warehouse.pallet'].create({'partner_id': self.customer.id, 'name': 'P1'})
        pallet2 = self.env['warehouse.pallet'].create({'partner_id': self.customer.id, 'name': 'P2'})

        # 4. Scan Product A twice (Split) and IGNORE Product B
        # Line 1: Prod A, 3 units -> Pallet 1
        self.env['warehouse.checking.line'].create({
            'session_id': session.id,
            'product_id': self.product.id,
            'purchase_order_id': po.id,
            'purchase_line_id': po.order_line[0].id,
            'open_qty': 10.0,
            'fulfill_qty': 3.0,
            'pallet_id': pallet1.id,
        })
        # Line 2: Prod A, 4 units -> Pallet 2
        self.env['warehouse.checking.line'].create({
            'session_id': session.id,
            'product_id': self.product.id,
            'purchase_order_id': po.id,
            'purchase_line_id': po.order_line[0].id,
            'open_qty': 7.0,
            'fulfill_qty': 4.0,
            'pallet_id': pallet2.id,
        })
        # Note: Product B is NOT added to session lines at all.

        # 5. Validate
        session.action_validate()

        # 6. Verify Results
        
        # A. Original Picking should be done
        self.assertEqual(picking.state, 'done', "Original picking should be validated")
        
        # B. Product A should have aggregated qty (3 + 4 = 7)
        move_a = picking.move_ids.filtered(lambda m: m.product_id == self.product)
        self.assertEqual(move_a.quantity, 7.0, "Product A should have 7 units processed")
        
        # C. Product B should have 0 units processed (or be cancelled/removed from done picking)
        move_b = picking.move_ids.filtered(lambda m: m.product_id == product_b)
        self.assertTrue(not move_b or move_b.quantity == 0.0 or move_b.state == 'cancel', 
                        "Product B should have 0 units processed in the done picking")

        # D. Backorder should exist
        backorder = po.picking_ids.filtered(lambda p: p.state not in ['done', 'cancel'])
        self.assertTrue(backorder, "A backorder should be created")
        
        # E. Backorder content verification
        bo_move_a = backorder.move_ids.filtered(lambda m: m.product_id == self.product)
        bo_move_b = backorder.move_ids.filtered(lambda m: m.product_id == product_b)
        
        self.assertEqual(bo_move_a.product_uom_qty, 3.0, "Backorder should have remaining 3 units of A")
        self.assertEqual(bo_move_b.product_uom_qty, 10.0, "Backorder should have all 10 units of B")

    def test_reset_preserves_fully_fulfilled_product(self):
        """
        Test that resetting a session where Product A was 100% done 
        and Product B was 0% done results in a single picking containing both.
        """
        # 1. Create Product B
        product_b = self.env['product.product'].create({
            'name': 'Product B', 'type': 'consu', 'is_storable': True, 'default_code': 'SKU-B'
        })

        # 2. Create PO with A and B (10 units each)
        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'order_line': [
                Command.create({'product_id': self.product.id, 'product_qty': 10.0}),
                Command.create({'product_id': product_b.id, 'product_qty': 10.0}),
            ]
        })
        po.button_confirm()
        original_picking = po.picking_ids[0]

        # 3. Session: Fulfill Product A 100% (Product B remains 0%)
        session = self.env['warehouse.checking.session'].create({'partner_id': self.vendor.id})
        pallet = self.env['warehouse.pallet'].create({'partner_id': self.customer.id, 'name': 'P1'})
        
        self.env['warehouse.checking.line'].create({
            'session_id': session.id,
            'product_id': self.product.id,
            'purchase_order_id': po.id,
            'purchase_line_id': po.order_line[0].id,
            'fulfill_qty': 10.0, # Full
            'open_qty': 10.0,
            'pallet_id': pallet.id,
        })
        session.action_validate()

        # At this point: 
        # - Picking 1 is DONE (Product A)
        # - Picking 2 is READY (Product B - Backorder)
        self.assertEqual(len(po.picking_ids.filtered(lambda p: p.state != 'cancel')), 2)

        # 4. RESET TO DRAFT
        session.action_reset_draft()

        # 5. Verification
        # There should now be only ONE active picking again
        active_pickings = po.picking_ids.filtered(lambda p: p.state not in ['done', 'cancel'])
        self.assertEqual(len(active_pickings), 1, "Should have merged back into one picking")
        
        main_picking = active_pickings[0]
        move_a = main_picking.move_ids.filtered(lambda m: m.product_id == self.product)
        move_b = main_picking.move_ids.filtered(lambda m: m.product_id == product_b)
        
        self.assertEqual(move_a.product_uom_qty, 10.0, "Product A should be back in the picking")
        self.assertEqual(move_b.product_uom_qty, 10.0, "Product B should still be in the picking")

    def test_scan_ignores_fully_received_items(self):
        """ Test that already received items do not appear in the scan wizard """
        # 1. Fully receive the item in Session 1
        session1 = self.env['warehouse.checking.session'].create({
            'partner_id': self.vendor.id
        })
        self._scan_and_add(session1, 'SKU001')
        
        line = session1.line_ids[0]
        line.fulfill_qty = 10.0
        pallet = self.env['warehouse.pallet'].create({
            'partner_id': self.customer.id,
            'name': 'PLT-TEST-FULL'
        })
        line.pallet_id = pallet
        
        session1.action_validate()
        
        po_line = self.po.order_line[0]
        self.assertEqual(po_line.qty_received, 10.0, "PO Line should be fully received")

        # 2. Try to scan again in a NEW session
        session2 = self.env['warehouse.checking.session'].create({
            'partner_id': self.vendor.id
        })
        session2.scan_sku = 'SKU001'
        
        # Since open_qty (qty_split - qty_received) is 0, it should raise the UserError
        with self.assertRaises(UserError) as cm:
            session2.action_search_sku()
            
        self.assertIn("already in the session or completed", str(cm.exception))

    def test_scan_includes_partially_received_items(self):
        """ Test that partially received items still appear but only for the remaining quantity """
        session1 = self.env['warehouse.checking.session'].create({
            'partner_id': self.vendor.id
        })
        self._scan_and_add(session1, 'SKU001')
        
        line = session1.line_ids[0]
        line.fulfill_qty = 4.0
        pallet = self.env['warehouse.pallet'].create({
            'partner_id': self.customer.id,
            'name': 'PLT-TEST-PARTIAL'
        })
        line.pallet_id = pallet
        
        session1.action_validate()
        
        po_line = self.po.order_line[0]
        po_line.invalidate_recordset()
        self.assertEqual(po_line.qty_received, 4.0)

        # 2. Scan again in a NEW session
        session2 = self.env['warehouse.checking.session'].create({
            'partner_id': self.vendor.id
        })
        
        self._scan_and_add(session2, 'SKU001')
        
        # It should have added a line with the remaining 6.0 units
        self.assertEqual(len(session2.line_ids), 1)
        self.assertEqual(session2.line_ids[0].open_qty, 6.0, "Remaining open quantity should be 6.0")
        self.assertEqual(session2.line_ids[0].fulfill_qty, 6.0)
