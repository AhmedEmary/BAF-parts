"""Local cache of Inter Cars' ProductInformation CSV.

IC ships a nightly ``ProductInformation`` CSV containing every product
they distribute — SKU, index, TecDoc article number, aftermarket
brand, description, EANs, package dimensions. It replaces most of
what ``/ic/catalog/products`` returns, is a lot faster than the
live API, and — crucially — lets us enumerate aftermarket
equivalents of a BAF OEM part locally (products that share a
``tec_doc`` value are equivalents).

The CSV has ~1.7M rows and is ~350 MB uncompressed, so we bulk-load
via psycopg ``COPY`` rather than the ORM. Everything is stored as
TEXT (the source CSV uses comma decimals; we cast in-flight when
needed rather than reject rows on import).
"""

import io
import logging
import re
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

    # Normalised copies of the identifier columns. IC's catalog often
    # carries the OEM number as ``47 22 07 10 09051`` while BAF's own
    # SKU is ``44772071009051`` — same physical part, different
    # formatting. We strip whitespace and common separators
    # (``. _ -``) and uppercase both sides before joining. These are
    # populated on import by bulk_load_csv, then indexed.
    n_tow_kod = fields.Char(string="Normalised IC SKU", index=True)
    n_ic_index = fields.Char(string="Normalised IC Index", index=True)
    n_tec_doc = fields.Char(string="Normalised TecDoc ArtNr", index=True)
    n_article = fields.Char(
        string="Normalised Article Number", index=True,
    )

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

        # Populate the normalised identifier columns. Doing this
        # in-database (as a single UPDATE) rather than during COPY
        # keeps the loader simple and lets Postgres run the regexp
        # in a single sequential pass.
        _logger.info("ic.product.info: normalising identifiers")
        cr.execute(
            r"""
            UPDATE ic_product_info SET
              n_tow_kod  = regexp_replace(upper(tow_kod),        '[\s._\-]', '', 'g'),
              n_ic_index = regexp_replace(upper(ic_index),       '[\s._\-]', '', 'g'),
              n_tec_doc  = regexp_replace(upper(tec_doc),        '[\s._\-]', '', 'g'),
              n_article  = regexp_replace(upper(article_number), '[\s._\-]', '', 'g')
            """
        )
        cr.execute("ANALYZE ic_product_info")
        rows = self.search_count([])
        elapsed = time.time() - started
        _logger.info(
            "ic.product.info bulk_load_csv: %d rows in %.1fs, replace=%s",
            rows, elapsed, replace,
        )
        return {'rows': rows, 'seconds': elapsed, 'columns': header_cols}

    @api.model
    def _norm(self, value):
        """Same normalisation Postgres applies to the ``n_*`` columns."""
        if not value:
            return ''
        return re.sub(r'[\s._\-]', '', value.upper())

    # ── Query API used by the shop-side resolver ─────────────────────────
    @api.model
    def find_equivalents(self, baf_sku, limit=32):
        """Return IC rows that are aftermarket equivalents of BAF's SKU.

        Two-stage search on **normalised** identifiers:

          1. Find rows where BAF's normalised SKU matches any IC
             identifier (``n_tow_kod``, ``n_ic_index``, ``n_article``,
             ``n_tec_doc``). These are the direct hits.
          2. Group by ``tec_doc`` (raw) and include any other IC row
             sharing that TecDoc — those are the aftermarket brand
             variants of the same underlying part.

        Results are sorted so ``OE <brand>`` rows come first (they are
        IC reselling the original manufacturer part, the highest-
        confidence match), then aftermarket brands, then alphabetical.
        """
        if not baf_sku:
            return []
        n = self._norm(baf_sku)
        if not n:
            return []
        cr = self.env.cr
        cr.execute(
            """
            WITH direct AS (
                SELECT id, tec_doc
                FROM ic_product_info
                WHERE n_tow_kod  = %(n)s
                   OR n_ic_index = %(n)s
                   OR n_article  = %(n)s
                   OR n_tec_doc  = %(n)s
                LIMIT 200
            )
            SELECT tow_kod, ic_index, tec_doc, tec_doc_prod, article_number,
                   manufacturer, short_description, description, barcodes,
                   package_weight, package_length, package_width, package_height
            FROM ic_product_info
            WHERE id IN (SELECT id FROM direct)
               OR tec_doc IN (
                    SELECT tec_doc FROM direct WHERE tec_doc <> ''
               )
            ORDER BY
              (manufacturer LIKE 'OE %%') DESC,
              manufacturer,
              tow_kod
            LIMIT %(l)s
            """,
            {'n': n, 'l': limit},
        )
        keys = [
            'tow_kod', 'ic_index', 'tec_doc', 'tec_doc_prod',
            'article_number', 'manufacturer', 'short_description',
            'description', 'barcodes', 'package_weight',
            'package_length', 'package_width', 'package_height',
        ]
        return [dict(zip(keys, row)) for row in cr.fetchall()]
