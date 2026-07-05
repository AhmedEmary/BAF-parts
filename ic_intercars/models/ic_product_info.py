"""Local cache of Inter Cars' ProductInformation CSV.

IC ships a nightly ``ProductInformation`` CSV containing every product
they distribute — SKU, index, TecDoc article number, manufacturer,
description, EANs, package dimensions. It replaces most of what
``/ic/catalog/products`` returns and is a lot faster than the live
API. From here, staff pick rows and turn them into normal Odoo
products (flagged ``aftermarket``) via :meth:`action_create_products`.

The CSV has ~1.7M rows and is ~350 MB uncompressed, so we bulk-load
via psycopg ``COPY`` rather than the ORM. Everything is stored as
TEXT (the source CSV uses comma decimals; we cast in-flight when
needed rather than reject rows on import).
"""

import io
import logging
import time

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# CSV header IC ships today. Order matters — the CSV is
# semicolon-separated with these columns in this sequence.
_CSV_COLUMNS = [
    'tow_kod', 'ic_index', 'tec_doc', 'tec_doc_prod',
    'article_number', 'manufacturer', 'short_description',
    'description', 'barcodes', 'package_weight',
    'package_length', 'package_width', 'package_height',
    'custom_code', 'blocked_return',
]
# Optional columns per IC docs — we accept them if present, ignore
# otherwise; the loader introspects the header line.
_OPTIONAL_COLUMNS = ['eprel_link', 'unit', 'quantity']

_CHUNK_ROWS = 50_000


class IcProductInfo(models.Model):
    """One row per IC ProductInformation entry."""

    _name = 'ic.product.info'
    _description = 'Inter Cars — Product Information Cache'
    _rec_name = 'tow_kod'
    _order = 'tow_kod'

    tow_kod = fields.Char(string="IC SKU", index=True, required=True)
    ic_index = fields.Char(string="IC Index", index=True)
    tec_doc = fields.Char(string="TecDoc ArtNr", index=True)
    tec_doc_prod = fields.Char(string="TecDoc Manufacturer ID")
    article_number = fields.Char(string="Article Number", index=True)
    manufacturer = fields.Char(string="Manufacturer", index=True)
    short_description = fields.Char(string="Short Description")
    description = fields.Text(string="Description")
    barcodes = fields.Char(
        string="EANs (comma-separated)", index=True,
        help="Some rows carry more than one EAN — comma-separated.",
    )
    package_weight = fields.Char(string="Weight (kg)")
    package_length = fields.Char(string="Length (cm)")
    package_width = fields.Char(string="Width (cm)")
    package_height = fields.Char(string="Height (cm)")
    custom_code = fields.Char(string="Customs Code")
    blocked_return = fields.Char(string="Blocked Return")
    # Optional columns per IC's docs — present in some feed
    # configurations. Must exist here or COPY crashes when IC turns
    # them on for BAF's aggregation.
    eprel_link = fields.Char(string="EPREL Link")
    unit = fields.Char(string="Unit of Measure")
    quantity = fields.Char(string="Unit Quantity")

    _tow_kod_uniq = models.Constraint(
        'unique(tow_kod)',
        "One row per IC SKU (tow_kod).",
    )

    # ── Bulk load ────────────────────────────────────────────────────────
    @api.model
    def bulk_load_csv(self, csv_bytes, replace=True):
        """Load ProductInformation CSV via ``COPY``.

        ``csv_bytes`` is the raw file content (semicolon-separated,
        headers on line 1). ``replace=True`` truncates the table first
        — this matches IC's own daily-refresh model where each dump
        is a full snapshot.

        Returns a stats dict: ``{'rows': N, 'seconds': X, 'columns': [...]}``.
        """
        started = time.time()

        # Peek at the header line to know which optional columns are
        # present. We can't trust "we know the columns" statically —
        # IC may add optional ones at any time.
        head = csv_bytes[:4096].split(b'\n', 1)[0].decode(
            'utf-8', errors='replace',
        )
        header_cols = [c.strip().lower() for c in head.split(';')]
        expected = _CSV_COLUMNS + [
            c for c in _OPTIONAL_COLUMNS if c in header_cols
        ]
        # We only COPY into columns the CSV actually has and we can
        # persist. Unknown headers → refuse loudly so silent drops
        # don't happen.
        unknown = [c for c in header_cols if c not in expected]
        if unknown:
            raise UserError(_(
                "Unknown columns in ProductInformation CSV header: %s. "
                "Update ic.product.info if IC has changed the schema."
            ) % ', '.join(unknown))
        missing = [c for c in _CSV_COLUMNS if c not in header_cols]
        if missing:
            raise UserError(_(
                "ProductInformation CSV is missing required columns: %s"
            ) % ', '.join(missing))

        cr = self.env.cr
        if replace:
            cr.execute("TRUNCATE TABLE ic_product_info RESTART IDENTITY")

        # Stream the bytes to psycopg's copy_expert. This is a single
        # server round-trip and does *not* materialise 350 MB in
        # Python. We use the header_cols in file order — the CSV's
        # order — so no column mapping is needed.
        column_list = ', '.join(header_cols)
        sql = (
            f"COPY ic_product_info ({column_list}) "
            f"FROM STDIN WITH (FORMAT csv, DELIMITER ';', HEADER true, QUOTE '\"')"
        )
        # psycopg2's copy_expert / psycopg3's copy — Odoo 19 uses
        # psycopg2 today, but the connection is exposed as
        # ``cr._cnx``. Fall back to writing chunks if the driver
        # doesn't like our BytesIO.
        try:
            cr._cnx.cursor().copy_expert(sql, io.BytesIO(csv_bytes))
        except AttributeError:
            # Odoo 19 alpha may expose psycopg3 — use ``copy`` there.
            with cr._cnx.cursor().copy(sql) as cp:
                for chunk_start in range(0, len(csv_bytes), 1 << 20):
                    cp.write(csv_bytes[chunk_start:chunk_start + (1 << 20)])

        cr.execute("ANALYZE ic_product_info")
        rows = self.search_count([])
        elapsed = time.time() - started
        _logger.info(
            "ic.product.info bulk_load_csv: %d rows in %.1fs, replace=%s",
            rows, elapsed, replace,
        )
        return {'rows': rows, 'seconds': elapsed, 'columns': header_cols}

    # ── Turn cache rows into normal Odoo products ────────────────────────
    @staticmethod
    def _to_float(value):
        """Parse IC's comma-decimal TEXT columns ('0,378' → 0.378)."""
        if not value:
            return 0.0
        try:
            return float(str(value).replace(',', '.'))
        except ValueError:
            return 0.0

    def _to_ic_data(self):
        """Shape one cache row like an item of ``/ic/catalog/products``
        so ``_baf_find_or_create_ic`` can consume it unchanged."""
        self.ensure_one()
        eans = [e.strip() for e in (self.barcodes or '').split(',')
                if e.strip()]
        return {
            'sku': self.tow_kod,
            'index': self.ic_index or '',
            'brand': self.manufacturer or '',
            'shortDescription': self.short_description or '',
            'description': self.description or '',
            'articleNumber': self.article_number or '',
            'eans': eans,
            'packageWeight': self._to_float(self.package_weight),
            'packageLength': self._to_float(self.package_length),
            'packageWidth': self._to_float(self.package_width),
            'packageHeight': self._to_float(self.package_height),
        }

    def action_create_products(self):
        """Create a normal (aftermarket-flagged) product per selected row.

        Live IC prices are quoted in batches of 100 first; rows IC
        refuses to quote are skipped — a product must never enter the
        catalog with a zero cost/price. Already-materialised SKUs are
        refreshed (cost + customer price), not duplicated.
        """
        backend = self.env['ic.backend']._get_default()
        if not backend:
            raise UserError(_(
                "No active Inter Cars backend. Configure one under "
                "Purchase → Configuration → Inter Cars."
            ))
        client = backend.get_client()
        rows = self.filtered('tow_kod')

        price_by_sku = {}
        skus = rows.mapped('tow_kod')
        for start in range(0, len(skus), 100):
            chunk = skus[start:start + 100]
            try:
                quote = client.get_price(
                    lines=[{'sku': s, 'quantity': 1} for s in chunk],
                    ship_to=backend.ship_to or None,
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning("IC price quote failed for chunk: %s", exc)
                continue
            for line in (quote.get('lines') or []):
                price = line.get('price') or {}
                if line.get('sku') and price.get('customerPriceNet') is not None:
                    price_by_sku[line['sku']] = float(price['customerPriceNet'])

        Product = self.env['product.product']
        created, refreshed, skipped = 0, 0, []
        for row in rows:
            price_net = price_by_sku.get(row.tow_kod)
            if price_net is None:
                skipped.append(row.tow_kod)
                continue
            existed = bool(Product._baf_find_ic_product(row.tow_kod))
            Product._baf_find_or_create_ic(
                backend, row._to_ic_data(), price_net=price_net,
            )
            if existed:
                refreshed += 1
            else:
                created += 1

        msg = _(
            "%(created)d product(s) created, %(refreshed)d refreshed."
        ) % {'created': created, 'refreshed': refreshed}
        if skipped:
            msg += _(
                "\nSkipped %(n)d SKU(s) IC would not quote: %(skus)s"
            ) % {'n': len(skipped), 'skus': ', '.join(skipped[:20])}
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Inter Cars Products"),
                'message': msg,
                'type': 'warning' if skipped else 'success',
                'sticky': bool(skipped),
            },
        }
