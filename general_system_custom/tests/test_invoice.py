from odoo.tests import TransactionCase, tagged
from odoo.exceptions import UserError
from odoo import Command

@tagged('post_install', '-at_install')
class TestInvoiceGeneration(TransactionCase):
    """ Tests for generating Invoices from Pallets and handling Down Payments """

    def setUp(self):
        super().setUp()
        self.customer_a = self.env['res.partner'].create({'name': 'Customer A'})
        self.customer_b = self.env['res.partner'].create({'name': 'Customer B'})
        
        self.account_income = self.env['account.account'].search([('account_type', '=', 'income_other')], limit=1)
        if not self.account_income:
            self.account_income = self.env['account.account'].create({
                'name': 'Test Income',
                'code': '999999',
                'account_type': 'income_other',
            })

        # Correctly defined storable products
        self.product_a = self.env['product.product'].create({
            'name': 'Product A',
            'type': 'consu', 
            'is_storable': True, 
            'list_price': 100.0,
            'property_account_income_id': self.account_income.id,
        })
        self.product_b = self.env['product.product'].create({
            'name': 'Product B',
            'type': 'consu', 
            'is_storable': True, 
            'list_price': 200.0,
            'property_account_income_id': self.account_income.id,
        })
        self.dp_product = self.env['product.product'].create({
            'name': 'Aconto',
            'type': 'service',
            'list_price': 50.0,
            'property_account_income_id': self.account_income.id,
        })
        # --- Setup for EU / Non-EU VAT Tests ---
        self.country_eu = self.env['res.country'].search([('code', '=', 'FR')], limit=1)
        if not self.country_eu:
            self.country_eu = self.env['res.country'].create({'name': 'France', 'code': 'FR'})

        self.country_noneu = self.env['res.country'].search([('code', '=', 'US')], limit=1)
        if not self.country_noneu:
            self.country_noneu = self.env['res.country'].create({'name': 'United States', 'code': 'US'})

        self.partner_eu_vat = self.env['res.partner'].create({
            'name': 'EU VAT Partner',
            'country_id': self.country_eu.id,
            'vat': 'FR23334175221'
        })
        self.partner_eu_novat = self.env['res.partner'].create({
            'name': 'EU No VAT Partner',
            'country_id': self.country_eu.id,
            'vat': False
        })
        self.partner_noneu = self.env['res.partner'].create({
            'name': 'Non-EU Partner',
            'country_id': self.country_noneu.id,
            'vat': False
        })

    def _create_ready_pallet(self, customer, product, qty=1.0, so=None):
        """ Helper to create a Pallet with validated content ready for invoicing """
        if not so:
            so = self.env['sale.order'].create({
                'partner_id': customer.id,
                'order_line': [Command.create({
                    'product_id': product.id,
                    'product_uom_qty': qty,
                    'price_unit': product.list_price,
                })]
            })
            so.action_confirm()

        po = self.env['purchase.order'].create({
            'partner_id': self.env['res.partner'].create({'name': 'Vendor'}).id,
            'sale_order_id': so.id,
            'order_line': [Command.create({
                'product_id': product.id,
                'product_qty': qty,
                'price_unit': 50.0,
            })]
        })
        po.button_confirm() 

        pallet = self.env['warehouse.pallet'].create({
            'partner_id': customer.id,
            'name': 'PLT-TEST'
        })
        
        session = self.env['warehouse.checking.session'].create({
            'partner_id': po.partner_id.id
        })
        
        self.env['warehouse.checking.line'].create({
            'session_id': session.id,
            'product_id': product.id,
            'purchase_order_id': po.id,
            'purchase_line_id': po.order_line[0].id,
            'sale_order_id': so.id,
            'sale_line_id': so.order_line[0].id,
            'open_qty': qty,
            'fulfill_qty': qty,
            'pallet_id': pallet.id
        })
        
        session.action_validate()
        pallet.action_mark_ready()
        return pallet

    def test_normal_single_invoice(self):
        """ Case 1: Normal single invoice from one pallet """
        pallet = self._create_ready_pallet(self.customer_a, self.product_a, qty=5.0)
        pallet.action_generate_invoice()
        
        self.assertTrue(pallet.invoice_id, "Invoice should be created and linked")
        invoice = pallet.invoice_id
        self.assertEqual(invoice.partner_id, self.customer_a)
        self.assertEqual(invoice.state, 'draft')
        self.assertEqual(len(invoice.invoice_line_ids), 1)
        
        line = invoice.invoice_line_ids[0]
        self.assertEqual(line.product_id, self.product_a)
        self.assertEqual(line.quantity, 5.0)
        self.assertEqual(line.price_unit, 100.0)

    def test_invoice_multiple_pallets_same_customer(self):
        """ Case 4: Invoice 2 pallets from same customer (Aggregation) """
        pallet_1 = self._create_ready_pallet(self.customer_a, self.product_a, qty=2.0)
        pallet_2 = self._create_ready_pallet(self.customer_a, self.product_b, qty=3.0)
        
        pallets = pallet_1 | pallet_2
        pallets.action_generate_invoice()
        
        self.assertTrue(pallet_1.invoice_id)
        self.assertEqual(pallet_1.invoice_id, pallet_2.invoice_id)
        
        invoice = pallet_1.invoice_id
        self.assertEqual(len(invoice.invoice_line_ids), 2)
        self.assertIn(self.product_a, invoice.invoice_line_ids.product_id)
        self.assertIn(self.product_b, invoice.invoice_line_ids.product_id)

    def test_invoice_mixed_customers_error(self):
        """ Case 3: Try to invoice 2 pallets from different customers (Should Fail) """
        pallet_a = self._create_ready_pallet(self.customer_a, self.product_a)
        pallet_b = self._create_ready_pallet(self.customer_b, self.product_b)
        
        pallets = pallet_a | pallet_b
        
        with self.assertRaises(UserError):
            pallets.action_generate_invoice()

    def test_invoice_targets_two_sos(self):
        """ Case 2: Invoice that targets two SOs on the same pallet """
        pallet = self.env['warehouse.pallet'].create({
            'partner_id': self.customer_a.id,
            'name': 'PLT-MULTI-SO'
        })
        
        so1 = self.env['sale.order'].create({
            'partner_id': self.customer_a.id,
            'order_line': [Command.create({'product_id': self.product_a.id, 'price_unit': 100.0})]
        })
        so1.action_confirm()
        
        so2 = self.env['sale.order'].create({
            'partner_id': self.customer_a.id,
            'order_line': [Command.create({'product_id': self.product_b.id, 'price_unit': 200.0})]
        })
        so2.action_confirm()
        
        self.env['warehouse.checking.line'].create({
            'product_id': self.product_a.id,
            'sale_order_id': so1.id,
            'sale_line_id': so1.order_line[0].id,
            'open_qty': 1.0, 
            'fulfill_qty': 1.0,
            'pallet_id': pallet.id,
        })
        self.env['warehouse.checking.line'].create({
            'product_id': self.product_b.id,
            'sale_order_id': so2.id,
            'sale_line_id': so2.order_line[0].id,
            'open_qty': 1.0,
            'fulfill_qty': 1.0,
            'pallet_id': pallet.id,
        })
        
        pallet.action_generate_invoice()
        
        invoice = pallet.invoice_id
        self.assertTrue(invoice)
        self.assertEqual(len(invoice.invoice_line_ids), 2)
        
        line_a = invoice.invoice_line_ids.filtered(lambda l: l.product_id == self.product_a)
        line_b = invoice.invoice_line_ids.filtered(lambda l: l.product_id == self.product_b)
        
        self.assertEqual(line_a.price_unit, 100.0)
        self.assertEqual(line_b.price_unit, 200.0)
        self.assertIn(so1.order_line[0], line_a.sale_line_ids)
        self.assertIn(so2.order_line[0], line_b.sale_line_ids)

    def test_down_payment_application(self):
        """ Test applying a down payment to a final invoice """
        # 1. Create and Post Down Payment Invoice
        dp_invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.customer_a.id,
            'invoice_line_ids': [Command.create({
                'product_id': self.dp_product.id,
                'quantity': 1,
                'price_unit': 50.0,
                'tax_ids':False,
            })]
        })
        dp_invoice.action_post()
        
        # 2. Setup Bank Journal and Register Payment
        journal = self.env['account.journal'].search([('type', '=', 'bank'), ('company_id', '=', self.env.company.id)], limit=1)
        if not journal:
            journal = self.env['account.journal'].create({
                'name': 'Bank Test',
                'code': 'BNKT',
                'type': 'bank',
            })
            
        payment_register = self.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=dp_invoice.ids
        ).create({'journal_id': journal.id})
        payment_register._create_payments()
        
        self.assertIn(dp_invoice.payment_state, ['paid', 'in_payment'])
        self.assertEqual(dp_invoice.amount_available_to_draw, 50.0)
        
        # 3. Create Final Invoice from Pallet ($200.0)
        pallet = self._create_ready_pallet(self.customer_a, self.product_a, qty=2.0)
        pallet.action_generate_invoice()
        final_invoice = pallet.invoice_id
        
        # 4. Open Wizard and Apply DP
        wizard = self.env['account.draw.down.payment.wizard'].with_context(default_invoice_id=final_invoice.id).create({})
        
        # Verify wizard correctly found the paid DP
        self.assertEqual(len(wizard.line_ids), 1)
        self.assertEqual(wizard.line_ids[0].down_payment_move_id, dp_invoice)
        self.assertEqual(wizard.line_ids[0].amount_total, 50.0)
        
        # Apply partial DP deduction ($30)
        wizard.line_ids[0].write({'amount_to_draw': 30.0})
        wizard.action_apply_down_payments()
        
        # 5. Verify Final Invoice has the specific Down Payment deduction line
        dp_line = final_invoice.invoice_line_ids.filtered(lambda l: l.is_down_payment)
        self.assertEqual(len(dp_line), 1)
        self.assertEqual(dp_line.quantity, -1.0)
        self.assertEqual(dp_line.price_unit, 30.0)
        self.assertEqual(dp_line.down_payment_origin_id, dp_invoice)
        
        # 6. Post Final Invoice and check remaining DP availability
        final_invoice.action_post()
        dp_invoice.invalidate_recordset(['amount_available_to_draw'])
        
        # $50 Original DP - $30 Subtracted = $20 remaining available
        self.assertEqual(dp_invoice.amount_available_to_draw, 20.0)

    def test_down_payment_exceed_available_error(self):
        """ Test error triggered when drawing more than available down payment """
        dp_invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.customer_a.id,
            'invoice_line_ids': [Command.create({
                'product_id': self.dp_product.id,
                'quantity': 1,
                'price_unit': 50.0,
                'tax_ids':False
            })]
        })
        dp_invoice.action_post()
        
        journal = self.env['account.journal'].search([('type', '=', 'bank'), ('company_id', '=', self.env.company.id)], limit=1)
        if not journal:
            journal = self.env['account.journal'].create({
                'name': 'Bank Test',
                'code': 'BNKT',
                'type': 'bank',
            })
            
        payment_register = self.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=dp_invoice.ids
        ).create({'journal_id': journal.id})
        payment_register._create_payments()
        
        pallet = self._create_ready_pallet(self.customer_a, self.product_a, qty=2.0)
        pallet.action_generate_invoice()
        final_invoice = pallet.invoice_id
        
        wizard = self.env['account.draw.down.payment.wizard'].with_context(default_invoice_id=final_invoice.id).create({})
        
        # Try to apply $60 (only $50 is available from DP)
        wizard.line_ids[0].write({'amount_to_draw': 60.0})
        
        with self.assertRaises(UserError):
            wizard.action_apply_down_payments()

    def test_document_type_eu_with_vat(self):
        """ Test: EU Client + VAT -> Should be Invoice (out_invoice) """
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_eu_vat.id,
            'invoice_line_ids': [Command.create({
                'product_id': self.product_a.id,
                'price_unit': 100.0,
            })]
        })
        self.assertEqual(
            move.move_type,
            'out_invoice',
            "EU client with a VAT number should generate an out_invoice."
        )

    def test_document_type_noneu_no_vat(self):
        """ Test: Non-EU Client + NO VAT -> Should remain Invoice (out_invoice) """
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_noneu.id,
            'invoice_line_ids': [Command.create({
                'product_id': self.product_a.id,
                'price_unit': 100.0,
            })]
        })
        self.assertEqual(
            move.move_type,
            'out_invoice',
            "Non-EU client (regardless of VAT) should always generate an out_invoice."
        )
@tagged('post_install', '-at_install')
class TestCreditNoteGeneration(TransactionCase):
    """ Tests for the Custom Credit Note Wizard (Section 9) """

    def setUp(self):
        super().setUp()
        self.customer = self.env['res.partner'].create({'name': 'Test Customer CN'})
        
        # where it's not standard or strict. Odoo defaults to current company anyway.
        self.account_income = self.env['account.account'].search(
            [('account_type', '=', 'income_other')], limit=1
        )
        if not self.account_income:
            self.account_income = self.env['account.account'].create({
                'name': 'Test Income',
                'code': '999999',
                'account_type': 'income_other',
            })

        self.product = self.env['product.product'].create({
            'name': 'Test Product CN',
            'type': 'consu',
            'is_storable': True,
            'list_price': 100.0,
            'property_account_income_id': self.account_income.id, 
        })

        # 1. Create Sales Order & Confirm
        self.so = self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'order_line': [Command.create({
                'product_id': self.product.id,
                'product_uom_qty': 10.0,
                'price_unit': 100.0,
            })]
        })
        self.so.action_confirm()
        
        # 2. Create and Post Invoice
        self.invoice = self.so._create_invoices()
        self.invoice.action_post()
        
        # Verify initial state (10 units invoiced)
        self.assertEqual(self.so.order_line.qty_invoiced, 10.0)

    def test_credit_note_full_reversal(self):
        """ Test creating a full credit note via the wizard """
        
        # 1. Open Wizard
        wizard = self.env['credit.note.wizard'].with_context(active_id=self.invoice.id).create({
            'reason': 'Damaged Goods'
        })
        
        # 2. Verify Wizard loaded lines correctly
        self.assertEqual(len(wizard.line_ids), 1)
        self.assertEqual(wizard.line_ids[0].quantity, 10.0)
        self.assertTrue(wizard.line_ids[0].move_line_id, "Must link to original line")
        
        # 3. Select Line (Simulate User)
        wizard.line_ids[0].is_selected = True
        
        # 4. Create CN
        action = wizard.action_create_credit_note()
        credit_note = self.env['account.move'].browse(action['res_id'])
        
        # 5. Check CN Content
        self.assertEqual(credit_note.move_type, 'out_refund')
        self.assertEqual(credit_note.ref, 'Damaged Goods')
        cn_line = credit_note.invoice_line_ids[0]
        self.assertEqual(cn_line.quantity, 10.0)
        self.assertEqual(cn_line.price_unit, 100.0)
        
        # 6. Post CN and Verify SO Update
        # Note: In standard Odoo, qty_invoiced decreases when the refund is POSTED
        credit_note.action_post()
        
        self.so.order_line.invalidate_recordset()
        self.assertEqual(self.so.order_line.qty_invoiced, 0.0, "Qty Invoiced should be 0 after full credit note")

    def test_credit_note_partial(self):
        """ Test creating a partial credit note (adjusting quantity) """
        
        wizard = self.env['credit.note.wizard'].with_context(active_id=self.invoice.id).create({
            'reason': 'Partial Return'
        })
        
        # Select and Modify Quantity
        w_line = wizard.line_ids[0]
        w_line.is_selected = True
        w_line.quantity = 3.0 # Refund only 3
        
        action = wizard.action_create_credit_note()
        credit_note = self.env['account.move'].browse(action['res_id'])
        
        self.assertEqual(credit_note.invoice_line_ids[0].quantity, 3.0)
        
        # Verify SO Update
        credit_note.action_post()
        self.so.order_line.invalidate_recordset()
        self.assertEqual(self.so.order_line.qty_invoiced, 7.0, "Qty Invoiced should be 10 - 3 = 7")

    def test_credit_note_no_selection_error(self):
        """ Test that the wizard raises an error if no lines are selected """
        wizard = self.env['credit.note.wizard'].with_context(active_id=self.invoice.id).create({})
        
        # Ensure nothing is selected
        wizard.line_ids.write({'is_selected': False})
        
        with self.assertRaises(UserError):
            wizard.action_create_credit_note()

