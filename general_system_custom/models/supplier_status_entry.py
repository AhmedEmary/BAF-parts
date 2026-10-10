import datetime
import re
from collections import Counter, defaultdict

from markupsafe import Markup
from odoo import models, fields, api

from .baf_import_utils import normalize_part_number
from .purchase_order_line import SUPPLIER_STATUS

# Remark keywords in supplier confirmations -> supplier status. Checked in
# order: a remark like "Entfallen, Rückstand storniert" is NLA, not backorder.
STATUS_KEYWORDS = [
    ('nla', ('entfallen', 'nla', 'no longer available')),
    ('not_orderable', ('nicht bestellbar', 'not orderable')),
    ('backorder', ('rückstand', 'rueckstand', 'ruckstand', 'backorder', 'back order')),
]

# 15.11.2026 / 15.11.26 / 15/11/2026
FULL_DATE_RE = re.compile(r'\b(\d{1,2})[./](\d{1,2})[./](\d{4}|\d{2})\b')
# 15.11. — only trusted inside a status remark (a price could look alike)
SHORT_DATE_RE = re.compile(r'\b(\d{1,2})\.(\d{1,2})\.(?!\d)')
# KW 47 / KW47/2026 (calendar week -> its Monday)
WEEK_RE = re.compile(r'\bkw\s*(\d{1,2})(?:\s*[/.-]\s*(\d{4}|\d{2}))?\b', re.I)

# A date without a year is the next occurrence, tolerating this much past.
YEARLESS_GRACE = datetime.timedelta(days=60)


def parse_status(text):
    lowered = (text or '').lower()
    for status, keywords in STATUS_KEYWORDS:
        if any(re.search(r'\b%s\b' % re.escape(k), lowered) for k in keywords):
            return status
    return False


def _full_year(year):
    return year + 2000 if year < 100 else year


def _next_occurrence(make_date, year, today):
    """make_date(year) for an explicit year, else the next occurrence."""
    if year:
        return make_date(_full_year(int(year)))
    found = make_date(today.year)
    if found < today - YEARLESS_GRACE:
        found = make_date(today.year + 1)
    return found


def parse_date(text, today):
    """Delivery date written in a remark: 20.11.2026, KW 47(/2026), or 20.11.
    when the remark also carries a status keyword. None when there is none."""
    text = text or ''
    candidates = [(lambda y, d=d, m=m: datetime.date(y, int(m), int(d)), year)
                  for d, m, year in FULL_DATE_RE.findall(text)]
    candidates += [(lambda y, w=week: datetime.date.fromisocalendar(y, int(w), 1), year)
                   for week, year in WEEK_RE.findall(text)]
    if parse_status(text):
        candidates += [(lambda y, d=d, m=m: datetime.date(y, int(m), int(d)), None)
                       for d, m in SHORT_DATE_RE.findall(text)]
    for make_date, year in candidates:
        try:
            return _next_occurrence(make_date, year, today)
        except ValueError:  # 31.02., KW 60, ...
            continue
    return None


class BafSupplierStatusEntry(models.Model):
    """One row of a supplier order confirmation, imported with Odoo's standard
    import (columns are mapped to these fields in the import screen, and the
    mapping is remembered per header name).

    Creating an entry applies it: the purchase lines with the same part number
    get the status / ETA, which /backorders shows to the customer. The order
    comes from a "Purchase Order" or "Sales Order" column, or — for supplier
    files that carry neither, like the BMW / MINI confirmations — from the PO
    the import was started on. Entries are kept as an import log.
    """
    _name = 'baf.supplier.status.entry'
    _description = 'Supplier Status Import Row'
    _order = 'id desc'
    _rec_name = 'part_number'

    purchase_id = fields.Many2one(
        'purchase.order', string='Purchase Order', index=True, ondelete='cascade',
        help="Leave unmapped when importing from a purchase order: that order is used.")
    sale_order_id = fields.Many2one(
        'sale.order', string='Sales Order', index=True, ondelete='cascade',
        help="Alternative to Purchase Order: the row applies to the purchase "
             "orders of this sales order.")
    part_number = fields.Char(
        string='Part Number',
        help="Matched against the SKU of the order lines, ignoring spaces and dashes.")
    remark = fields.Char(
        string='Remark',
        help="Supplier remark column. Entfallen = NLA, Rückstand = Backorder, "
             "nicht bestellbar = Not Orderable; a date in it (20.11.2026, KW 47) "
             "is read as the ETA. Plain values (a quantity, a country) mean confirmed.")
    status = fields.Selection(
        SUPPLIER_STATUS, string='Status',
        help="Map a status column here, or leave it to be read from the remark.")
    eta = fields.Date(
        string='ETA',
        help="Map a delivery date column here, or leave it to be read from the remark.")
    purchase_line_ids = fields.Many2many(
        'purchase.order.line', string='Updated Lines', readonly=True)
    result = fields.Selection([
        ('updated', 'Updated'),
        ('not_found', 'Part not on order'),
        ('no_order', 'No purchase order'),
        ('no_part', 'No part number'),
    ], string='Result', readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        today = fields.Date.context_today(self)
        for vals in vals_list:
            vals['status'] = vals.get('status') or parse_status(vals.get('remark'))
            vals['eta'] = vals.get('eta') or parse_date(vals.get('remark'), today) or False
            if vals['eta'] and not vals['status']:
                # A delivery date on its own means the part is still coming.
                vals['status'] = 'backorder'
        entries = super().create(vals_list)
        entries._apply_to_purchase_lines()
        entries._post_import_summary()
        return entries

    def _target_orders(self):
        self.ensure_one()
        return self.purchase_id or self.sale_order_id.purchase_ids.filtered(
            lambda po: po.state != 'cancel')

    def _apply_to_purchase_lines(self):
        index_by_orders = {}  # one part-number index per order set
        by_result = defaultdict(self.browse)
        for entry in self:
            part = normalize_part_number(entry.part_number)
            orders = entry._target_orders()
            if not part:
                by_result['no_part'] |= entry
                continue
            if not orders:
                by_result['no_order'] |= entry
                continue
            if orders not in index_by_orders:
                index_by_orders[orders] = orders._baf_order_lines_by_part()
            lines = index_by_orders[orders].get(part)
            if not lines:
                by_result['not_found'] |= entry
                continue
            lines.write({
                'baf_supplier_status': entry.status,
                'baf_eta': entry.eta,
                'baf_supplier_note': entry.remark,
            })
            entry.purchase_line_ids = lines
            by_result['updated'] |= entry
        for result, entries in by_result.items():
            entries.result = result

    def _post_import_summary(self):
        """One chatter note per purchase order this batch touched. When the
        import ran on a PO (its confirmation file), also list the lines the
        file left out and the rows for parts that are not on the order."""
        status_labels = dict(self._fields['status']._description_selection(self.env))
        confirmed = self.env._("Confirmed")
        for po in self.purchase_id | self.purchase_line_ids.order_id:
            updated = self.purchase_line_ids.filtered(lambda l: l.order_id == po)
            counts = Counter(status_labels.get(l.baf_supplier_status, confirmed) for l in updated)
            parts = [self.env._("Supplier status imported: %s line(s) updated.", len(updated))]
            parts += [f"{label}: {count}" for label, count in counts.items()]
            if po in self.purchase_id:
                missing = po.order_line.filtered('product_id') - updated
                if missing:
                    parts.append(self.env._("Not in file (unchanged): %s", ', '.join(
                        po._baf_display_sku(l.product_id) for l in missing)))
                not_found = self.filtered(lambda e: e.purchase_id == po and e.result == 'not_found')
                if not_found:
                    parts.append(self.env._("Parts not on this order: %s", ', '.join(not_found.mapped('part_number'))))
            po.message_post(body=Markup('<br/>').join(parts))
