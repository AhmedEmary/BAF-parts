from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBafVendorSelection(TransactionCase):
    """
    Covers the auto-vendor selection feature:
      - baf_get_purchase_price_details (extended price-engine output)
      - baf_get_best_vendor (cheapest pick + tie-breaks + pinning rules)
      - sale.order.line._compute_purchase_vendor_id (preselection)
      - baf.vendor.price.compare wizard (default_get + apply)
    """

    def setUp(self):
        super().setUp()

        Brand = self.env['product.brand']
        Partner = self.env['res.partner']
        Tmpl = self.env['product.template']
        Disc = self.env['baf.discount.line']

        # ── Brands ────────────────────────────────────────────────────────
        self.brand_bmw = Brand.create({'name': 'BMW'})
        self.brand_mini = Brand.create({'name': 'MINI'})
        self.brand_jag = Brand.create({'name': 'Jaguar'})
        self.brand_bosal = Brand.create({'name': 'Bosal'})

        # ── Vendors with BAF supplier codes + supplied brands ─────────────
        self.vendor_sup1 = Partner.create({
            'name': 'Vendor SUP1',
            'baf_supplier_code': 'SUP1',
            'baf_brand_ids': [(6, 0, [self.brand_bmw.id, self.brand_mini.id])],
        })
        self.vendor_sup2 = Partner.create({
            'name': 'Vendor SUP2',
            'baf_supplier_code': 'SUP2',
            'baf_brand_ids': [(6, 0, [self.brand_bmw.id, self.brand_mini.id])],
        })
        self.vendor_sup3 = Partner.create({
            'name': 'Vendor SUP3',
            'baf_supplier_code': 'SUP3',
            'baf_brand_ids': [(6, 0, [self.brand_bmw.id, self.brand_mini.id])],
        })
        self.vendor_jlr = Partner.create({
            'name': 'Vendor JLR',
            'baf_supplier_code': 'SUP_JLR',
            'baf_brand_ids': [(6, 0, [self.brand_jag.id])],
        })
        self.vendor_eu = Partner.create({
            'name': 'Vendor EU',
            'baf_supplier_code': 'EU_DIRECT',
            'baf_brand_ids': [(6, 0, [self.brand_bosal.id])],
        })
        self.vendor_no_baf = Partner.create({
            'name': 'Vendor without BAF code',
            # No baf_supplier_code, no baf_brand_ids → NOT eligible
        })

        # ── Discount table ────────────────────────────────────────────────
        # All "/_10" rows; chosen %s let us reason about prices easily on UPE 100.
        rows = [
            # BMW T12 — code 10 → distinct discounts so cheapest is well-defined
            ('purchase', 'SUP1_BMW_T12', '10', 20.0),  # → 80
            ('purchase', 'SUP2_BMW_T12', '10', 22.0),  # → 78
            ('purchase', 'SUP3_BMW_T12', '10', 24.0),  # → 76
            # BMW T12 — code 99 → all three suppliers same %, used for ties
            ('purchase', 'SUP1_BMW_T12', '99', 10.0),  # → 90
            ('purchase', 'SUP2_BMW_T12', '99', 10.0),  # → 90
            ('purchase', 'SUP3_BMW_T12', '99', 10.0),  # → 90
            # BMW T12 — code 23 → SUP2 = SUP3 (cheaper than SUP1)
            ('purchase', 'SUP1_BMW_T12', '23', 5.0),   # → 95
            ('purchase', 'SUP2_BMW_T12', '23', 15.0),  # → 85
            ('purchase', 'SUP3_BMW_T12', '23', 15.0),  # → 85
            # Motorcycle column (SUP3 only)
            ('purchase', 'SUP3_MOTO', '10', 40.0),     # → 60
            # JLR
            ('purchase', 'SUP_JLR_JLR', '1A', 35.0),   # → 65
        ]
        for table_type, column_key, code, pct in rows:
            Disc.create({
                'table_type': table_type,
                'column_key': column_key,
                'discount_code': code,
                'discount_pct': pct,
            })

        # ── Product factory ───────────────────────────────────────────────
        self.Tmpl = Tmpl

        def make(brand, sku, **extra):
            vals = {
                'name': f'{brand.name} {sku}',
                'brand': brand.id,
                'sku': sku,
                'list_price': 100.0,
                'baf_discount_code': '10',
                'baf_type_code': 1,
                'baf_mod': 'car',
                'supplier_route': 'de_table',
                'surcharge': 0.0,
                'type': 'consu',
            }
            vals.update(extra)
            tmpl = Tmpl.create(vals)
            return tmpl.product_variant_id

        self._make = make

        # Common fixtures
        self.bmw_default = make(self.brand_bmw, 'BMW-DEFAULT')                   # code 10
        self.bmw_tie_all = make(self.brand_bmw, 'BMW-TIE-ALL', baf_discount_code='99')
        self.bmw_tie23 = make(self.brand_bmw, 'BMW-TIE23', baf_discount_code='23')
        self.bmw_moto = make(self.brand_bmw, 'BMW-MOTO', baf_mod='motorcycle')
        self.jlr_product = make(self.brand_jag, 'JAG-1A', baf_discount_code='1A')
        self.eu_product = make(self.brand_bosal, 'BOSAL-1', supplier_route='eu_direct')
        # EU product has supplierinfo at €70 from vendor_eu
        self.eu_product.product_tmpl_id.write({
            'seller_ids': [Command.create({'partner_id': self.vendor_eu.id, 'price': 70.0})],
        })

        self.customer = Partner.create({'name': 'A Test Customer'})

    # ─────────────────────────────────────────────────────────────────────
    # baf_get_purchase_price_details
    # ─────────────────────────────────────────────────────────────────────

    def test_01_purchase_details_de_table_returns_full_breakdown(self):
        details = self.bmw_default.baf_get_purchase_price_details('SUP1')
        self.assertEqual(details['column_key'], 'SUP1_BMW_T12')
        self.assertAlmostEqual(details['discount_pct'], 20.0)
        self.assertAlmostEqual(details['price'], 80.0)
        self.assertEqual(details['sb_surcharge'], 0.0)
        self.assertEqual(details['pricing_method'], 'discount_table')

    def test_02_purchase_details_motorcycle_pinned_to_sup3_moto(self):
        # Even when called with SUP1, motorcycle products land on SUP3_MOTO
        details = self.bmw_moto.baf_get_purchase_price_details('SUP1')
        self.assertEqual(details['column_key'], 'SUP3_MOTO')
        self.assertAlmostEqual(details['price'], 60.0)

    def test_03_purchase_details_eu_direct_returns_none(self):
        self.assertIsNone(self.eu_product.baf_get_purchase_price_details('SUP1'))

    def test_04_purchase_details_no_column_key_returns_none(self):
        # Bosal product on de_table has no recognised brand column key
        bosal_de = self._make(self.brand_bosal, 'BOSAL-DE', supplier_route='de_table')
        self.assertEqual(bosal_de.baf_column_key, 'BOSAL')  # raw name
        # SUP1_BOSAL row not in table → discount_pct=0 → price = UPE; details still returned
        details = bosal_de.baf_get_purchase_price_details('SUP1')
        self.assertEqual(details['column_key'], 'SUP1_BOSAL')
        self.assertAlmostEqual(details['price'], 100.0)

    def test_05_purchase_price_legacy_wrapper_still_returns_float(self):
        # Backwards compat — old call signature returns just the price
        self.assertAlmostEqual(self.bmw_default.baf_get_purchase_price('SUP1'), 80.0)
        self.assertIsNone(self.eu_product.baf_get_purchase_price('SUP1'))

    # ─────────────────────────────────────────────────────────────────────
    # baf_get_best_vendor — winner / candidates / reasons
    # ─────────────────────────────────────────────────────────────────────

    def test_10_best_vendor_no_brand_returns_empty(self):
        no_brand = self._make(self.brand_bmw, 'NO-BRAND')
        no_brand.product_tmpl_id.brand = False
        result = no_brand.baf_get_best_vendor()
        self.assertFalse(result['vendor'])
        self.assertEqual(result['candidates'], [])
        self.assertIn("No vendor", result['reason'])

    def test_11_best_vendor_no_eligible_vendor_for_brand(self):
        # Mercedes brand exists on no vendor → empty
        brand_mb = self.env['product.brand'].create({'name': 'Mercedes'})
        mb_product = self._make(brand_mb, 'MB-1')
        result = mb_product.baf_get_best_vendor()
        self.assertFalse(result['vendor'])
        self.assertIn("Mercedes", result['reason'])

    def test_12_best_vendor_three_distinct_prices_picks_cheapest(self):
        # SUP1=80, SUP2=78, SUP3=76 → SUP3 wins
        result = self.bmw_default.baf_get_best_vendor()
        self.assertEqual(result['vendor'], self.vendor_sup3)
        self.assertAlmostEqual(result['price'], 76.0)
        self.assertEqual(result['method'], 'discount_table')
        self.assertEqual(len(result['candidates']), 3)
        winners = [c for c in result['candidates'] if c['is_winner']]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]['vendor'], self.vendor_sup3)

    def test_13_best_vendor_all_equal_picks_sup1(self):
        # All three discount 10% → all 90 → SUP1 wins on preference
        result = self.bmw_tie_all.baf_get_best_vendor()
        self.assertEqual(result['vendor'], self.vendor_sup1)
        self.assertAlmostEqual(result['price'], 90.0)
        self.assertIn("Tie", result['reason'])
        self.assertIn("SUP1", result['reason'])

    def test_14_best_vendor_sup2_equals_sup3_picks_sup2(self):
        # SUP1=95, SUP2=SUP3=85 → SUP2 wins on tie-break
        result = self.bmw_tie23.baf_get_best_vendor()
        self.assertEqual(result['vendor'], self.vendor_sup2)
        self.assertAlmostEqual(result['price'], 85.0)
        self.assertIn("Tie", result['reason'])
        self.assertIn("SUP2", result['reason'])

    def test_15_best_vendor_motorcycle_pins_to_sup3(self):
        # All three vendors stock BMW; motorcycle parts only allowed from SUP3
        result = self.bmw_moto.baf_get_best_vendor()
        self.assertEqual(result['vendor'], self.vendor_sup3)
        self.assertAlmostEqual(result['price'], 60.0)
        # Only ONE candidate — SUP1 / SUP2 are filtered out at the brand step
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(result['candidates'][0]['column_key'], 'SUP3_MOTO')
        self.assertIn("Motorcycle", result['reason'])

    def test_16_best_vendor_motorcycle_with_no_sup3_returns_empty(self):
        # Move SUP3 off BMW so no SUP3 vendor supplies the brand
        self.vendor_sup3.write({'baf_brand_ids': [(6, 0, [self.brand_mini.id])]})
        result = self.bmw_moto.baf_get_best_vendor()
        self.assertFalse(result['vendor'])
        self.assertIn("Supplier 3", result['reason'])

    def test_17_best_vendor_jlr_uses_sup_jlr_column(self):
        result = self.jlr_product.baf_get_best_vendor()
        self.assertEqual(result['vendor'], self.vendor_jlr)
        self.assertAlmostEqual(result['price'], 65.0)
        self.assertEqual(result['candidates'][0]['column_key'], 'SUP_JLR_JLR')

    def test_18_best_vendor_eu_direct_uses_supplierinfo(self):
        result = self.eu_product.baf_get_best_vendor()
        self.assertEqual(result['vendor'], self.vendor_eu)
        self.assertEqual(result['method'], 'supplierinfo')
        self.assertAlmostEqual(result['price'], 70.0)
        self.assertIn("supplier pricelist", result['reason'])

    def test_19_best_vendor_eu_direct_without_supplierinfo_is_unpriced(self):
        # eu_no_seller has no product.supplierinfo — vendor still listed but unpriced
        eu_no_seller = self._make(self.brand_bosal, 'BOSAL-NOSELLER', supplier_route='eu_direct')
        result = eu_no_seller.baf_get_best_vendor()
        self.assertFalse(result['vendor'])
        self.assertEqual(len(result['candidates']), 1)
        cand = result['candidates'][0]
        self.assertEqual(cand['vendor'], self.vendor_eu)
        self.assertIsNone(cand['price'])
        self.assertIn("No supplier pricelist entry", cand['note'])

    def test_20_best_vendor_mix_eu_cheaper_than_sup1(self):
        # Add BMW to the EU vendor and quote 70 €. SUP1/SUP2/SUP3 are 80/78/76.
        # EU at 70 < SUP3 at 76 → EU wins regardless of the product's de_table route.
        self.vendor_eu.write({'baf_brand_ids': [(4, self.brand_bmw.id)]})
        self.bmw_default.product_tmpl_id.write({
            'seller_ids': [Command.create({'partner_id': self.vendor_eu.id, 'price': 70.0})],
        })
        result = self.bmw_default.baf_get_best_vendor()
        self.assertEqual(result['vendor'], self.vendor_eu)
        self.assertEqual(result['method'], 'supplierinfo')
        self.assertAlmostEqual(result['price'], 70.0)

    def test_21_best_vendor_skips_partners_without_supplier_code(self):
        # vendor_no_baf has no baf_supplier_code → never eligible even if added to brand
        self.vendor_no_baf.write({
            'baf_brand_ids': [(6, 0, [self.brand_bmw.id])],
        })
        result = self.bmw_default.baf_get_best_vendor()
        self.assertNotIn(self.vendor_no_baf, [c['vendor'] for c in result['candidates']])

    def test_22_best_vendor_reason_mentions_winner_code_and_price(self):
        result = self.bmw_default.baf_get_best_vendor()
        self.assertIn('SUP3', result['reason'])
        self.assertIn('76', result['reason'])

    # ─────────────────────────────────────────────────────────────────────
    # sale.order.line preselection
    # ─────────────────────────────────────────────────────────────────────

    def test_30_sale_order_line_preselects_best_vendor(self):
        so = self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'order_line': [Command.create({
                'product_id': self.bmw_default.id,
                'product_uom_qty': 1.0,
            })],
        })
        line = so.order_line
        # Cheapest BMW vendor is SUP3 (price 76)
        self.assertEqual(line.purchase_vendor_id, self.vendor_sup3)

    def test_31_sale_order_line_falls_back_to_seller_ids_when_no_baf_vendor(self):
        # Product on Mercedes brand → no BAF vendor; classic seller_ids should kick in
        brand_mb = self.env['product.brand'].create({'name': 'Mercedes-Generic'})
        fallback_vendor = self.env['res.partner'].create({'name': 'Fallback'})
        mb = self._make(brand_mb, 'MB-FALLBACK')
        mb.product_tmpl_id.write({
            'seller_ids': [Command.create({'partner_id': fallback_vendor.id, 'price': 50.0})],
        })
        so = self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'order_line': [Command.create({
                'product_id': mb.id,
                'product_uom_qty': 1.0,
            })],
        })
        self.assertEqual(so.order_line.purchase_vendor_id, fallback_vendor)

    def test_32_sale_order_line_user_override_is_preserved(self):
        so = self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'order_line': [Command.create({
                'product_id': self.bmw_default.id,
                'product_uom_qty': 1.0,
            })],
        })
        line = so.order_line
        # User overrides preselection
        line.purchase_vendor_id = self.vendor_sup1
        self.assertEqual(line.purchase_vendor_id, self.vendor_sup1)
        # Touching another stored field doesn't recompute the vendor
        line.product_uom_qty = 5.0
        self.assertEqual(line.purchase_vendor_id, self.vendor_sup1)

    def test_33_sale_order_line_motorcycle_preselects_sup3(self):
        so = self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'order_line': [Command.create({
                'product_id': self.bmw_moto.id,
                'product_uom_qty': 1.0,
            })],
        })
        self.assertEqual(so.order_line.purchase_vendor_id, self.vendor_sup3)

    # ─────────────────────────────────────────────────────────────────────
    # Wizard
    # ─────────────────────────────────────────────────────────────────────

    def _make_so_with_line(self, product):
        so = self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'order_line': [Command.create({
                'product_id': product.id,
                'product_uom_qty': 3.0,
            })],
        })
        return so.order_line

    def test_40_wizard_default_get_populates_lines_for_each_candidate(self):
        line = self._make_so_with_line(self.bmw_default)
        wiz = self.env['baf.vendor.price.compare'].with_context(
            default_sale_line_id=line.id
        ).create({})

        self.assertEqual(wiz.sale_line_id, line)
        self.assertEqual(wiz.product_id, self.bmw_default)
        self.assertEqual(wiz.brand_id, self.brand_bmw)
        self.assertEqual(len(wiz.line_ids), 3)  # SUP1, SUP2, SUP3 — all BMW vendors

        winners = wiz.line_ids.filtered(lambda l: l.is_winner)
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners.vendor_id, self.vendor_sup3)
        self.assertAlmostEqual(winners.price, 76.0)
        self.assertEqual(winners.method, 'discount_table')

    def test_41_wizard_preselects_winner_in_selected_vendor(self):
        line = self._make_so_with_line(self.bmw_default)
        wiz = self.env['baf.vendor.price.compare'].with_context(
            default_sale_line_id=line.id
        ).create({})
        self.assertEqual(wiz.selected_vendor_id, self.vendor_sup3)
        self.assertEqual(set(wiz.selectable_vendor_ids.ids),
                         {self.vendor_sup1.id, self.vendor_sup2.id, self.vendor_sup3.id})

    def test_42_wizard_apply_writes_vendor_back_to_so_line(self):
        line = self._make_so_with_line(self.bmw_default)
        # Sanity: preselection on the SO line is SUP3
        self.assertEqual(line.purchase_vendor_id, self.vendor_sup3)

        wiz = self.env['baf.vendor.price.compare'].with_context(
            default_sale_line_id=line.id
        ).create({})
        wiz.selected_vendor_id = self.vendor_sup1
        result = wiz.action_apply()

        self.assertEqual(result['type'], 'ir.actions.act_window_close')
        self.assertEqual(line.purchase_vendor_id, self.vendor_sup1)

    def test_43_wizard_apply_without_selection_raises(self):
        line = self._make_so_with_line(self.bmw_default)
        wiz = self.env['baf.vendor.price.compare'].with_context(
            default_sale_line_id=line.id
        ).create({})
        wiz.selected_vendor_id = False
        with self.assertRaises(UserError):
            wiz.action_apply()

    def test_44_wizard_unpriced_eu_candidate_appears_but_marked_not_priceable(self):
        eu_no_seller = self._make(self.brand_bosal, 'BOSAL-NOSELLER-W', supplier_route='eu_direct')
        line = self._make_so_with_line(eu_no_seller)
        wiz = self.env['baf.vendor.price.compare'].with_context(
            default_sale_line_id=line.id
        ).create({})
        self.assertEqual(len(wiz.line_ids), 1)
        cand = wiz.line_ids
        self.assertEqual(cand.vendor_id, self.vendor_eu)
        self.assertFalse(cand.priceable)
        self.assertFalse(cand.is_winner)

    def test_45_wizard_motorcycle_lists_only_sup3(self):
        line = self._make_so_with_line(self.bmw_moto)
        wiz = self.env['baf.vendor.price.compare'].with_context(
            default_sale_line_id=line.id
        ).create({})
        self.assertEqual(len(wiz.line_ids), 1)
        self.assertEqual(wiz.line_ids.vendor_id, self.vendor_sup3)
        self.assertIn("Motorcycle", wiz.reason)

    def test_46_action_open_vendor_compare_returns_correct_action(self):
        line = self._make_so_with_line(self.bmw_default)
        action = line.action_open_vendor_compare()
        self.assertEqual(action['res_model'], 'baf.vendor.price.compare')
        self.assertEqual(action['target'], 'new')
        self.assertEqual(action['context']['default_sale_line_id'], line.id)

    # ─────────────────────────────────────────────────────────────────────
    # PO line snapshot fields (discount code, %, column key)
    # ─────────────────────────────────────────────────────────────────────

    def test_50_po_line_snapshots_baf_discount_fields_on_create(self):
        line = self._make_so_with_line(self.bmw_default)
        line.action_create_purchase_order()
        po_line = line.order_id.purchase_ids.order_line
        self.assertEqual(len(po_line), 1)
        # Auto vendor is SUP3 → cheapest BMW T12 column → 24%
        self.assertEqual(po_line.baf_discount_code, '10')
        self.assertAlmostEqual(po_line.baf_discount_pct, 24.0)
        self.assertEqual(po_line.baf_column_key, 'SUP3_BMW_T12')
        self.assertAlmostEqual(po_line.price_unit, 76.0)

    def test_51_po_line_snapshots_recompute_on_retail_or_surcharge_write(self):
        line = self._make_so_with_line(self.bmw_default)
        line.action_create_purchase_order()
        po_line = line.order_id.purchase_ids.order_line
        # Touching surcharge re-runs the BAF engine and refreshes the snapshot
        po_line.surcharge = 1.23
        self.assertEqual(po_line.baf_column_key, 'SUP3_BMW_T12')
        self.assertAlmostEqual(po_line.baf_discount_pct, 24.0)

    def test_52_po_line_eu_direct_clears_baf_snapshot(self):
        # EU product needs purchase_vendor_id pointing at vendor_eu for the
        # line-level create-PO method to find a vendor.
        line = self._make_so_with_line(self.eu_product)
        line.purchase_vendor_id = self.vendor_eu
        line.action_create_purchase_order()
        po_line = line.order_id.purchase_ids.order_line
        # EU_DIRECT path: no discount table → no column / %
        self.assertFalse(po_line.baf_column_key)
        self.assertEqual(po_line.baf_discount_pct, 0.0)
        # Discount code is still snapshotted from the product
        self.assertEqual(po_line.baf_discount_code, '10')
