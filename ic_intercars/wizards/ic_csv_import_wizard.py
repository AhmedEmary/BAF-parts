"""Two-mode CSV importer for the IC ProductInformation feed.

Mode A — **Upload**: paste the raw ``.csv`` (or its ``.zip``) into the
wizard's file field and click Import. Streams the CSV directly into
Postgres via ``COPY``.

Mode B — **Fetch from IC**: use the ``csv_login`` / ``csv_password``
stored on the active ``ic.backend`` to download today's file from
``https://data.webapi.intercars.eu/customer/<login>/ProductInformation/``.
Same COPY on the way in.

The wizard reports rows inserted + seconds elapsed, and (optionally)
auto-populates ``ic_seed_sku`` on every BAF template whose ``sku``
lines up with an IC identifier — that's what makes the shop-side
equivalents block actually render alternatives after an import.
"""

import base64
import io
import logging
import zipfile
from datetime import timedelta

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_IC_CSV_ROOT = 'https://data.webapi.intercars.eu/customer'
_MAX_FETCH_ATTEMPTS = 7  # walk back up to a week if today isn't ready yet


class IcCsvImportWizard(models.TransientModel):
    _name = 'ic.csv.import.wizard'
    _description = 'Import IC ProductInformation CSV'

    backend_id = fields.Many2one(
        'ic.backend', string="Backend",
        default=lambda self: self.env['ic.backend']._get_default(),
        required=True,
    )
    source = fields.Selection([
        ('upload', 'Upload File'),
        ('fetch', 'Fetch from Inter Cars (using backend CSV credentials)'),
    ], default='upload', required=True)

    upload_file = fields.Binary(
        string="ProductInformation CSV/ZIP",
        help="The ProductInformation_YYYY-MM-DD.csv or .csv.zip file.",
    )
    upload_filename = fields.Char(string="Filename")

    auto_map_seeds = fields.Boolean(
        string="Auto-populate IC Seed SKU on matching BAF templates",
        default=True,
        help="After import, run the OEM ↔ IC cross-match. Any BAF "
             "product.template whose sku matches an IC identifier "
             "(tow_kod / ic_index / tec_doc / article_number) will get "
             "ic_seed_sku filled with the matching IC SKU. Templates "
             "that already have a seed are left alone.",
    )

    # ── Action ───────────────────────────────────────────────────────────
    def action_import(self):
        self.ensure_one()
        if not self.backend_id:
            raise UserError(_(
                "No active IC backend. Configure one under "
                "Purchase → Configuration → Inter Cars."
            ))
        csv_bytes = self._resolve_bytes()
        stats = self.env['ic.product.info'].sudo().bulk_load_csv(
            csv_bytes, replace=True,
        )

        mapped = 0
        if self.auto_map_seeds:
            mapped = self._auto_map_seeds()

        msg = _(
            "Imported %(rows)d IC products in %(secs).1fs.\n"
            "Auto-mapped ic_seed_sku on %(mapped)d BAF templates."
        ) % {
            'rows': stats['rows'],
            'secs': stats['seconds'],
            'mapped': mapped,
        }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('IC CSV Imported'),
                'message': msg,
                'type': 'success',
                'sticky': True,
            },
        }

    # ── Bytes resolution (upload OR HTTP fetch) ──────────────────────────
    def _resolve_bytes(self):
        self.ensure_one()
        if self.source == 'upload':
            if not self.upload_file:
                raise UserError(_(
                    "Attach a ProductInformation CSV or ZIP file."
                ))
            raw = base64.b64decode(self.upload_file)
            return self._unzip_if_needed(raw, self.upload_filename or '')
        # source == 'fetch'
        return self._fetch_from_ic()

    def _unzip_if_needed(self, raw, filename):
        if raw[:2] == b'PK':  # ZIP magic
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                # Find the CSV member — IC ships one per zip.
                members = [
                    n for n in zf.namelist()
                    if n.lower().endswith('.csv')
                ]
                if not members:
                    raise UserError(_(
                        "ZIP file %s contains no .csv member."
                    ) % filename)
                return zf.read(members[0])
        return raw

    def _fetch_from_ic(self):
        self.ensure_one()
        backend = self.backend_id
        if not backend.csv_login or not backend.csv_password:
            raise UserError(_(
                "IC CSV credentials are missing on the backend "
                "(fields 'CSV Login' and 'CSV Password')."
            ))

        base = (
            f"{_IC_CSV_ROOT}/{backend.csv_login}/ProductInformation"
        )
        auth = (backend.csv_login, backend.csv_password)

        # IC generates today's file overnight — if it isn't ready
        # yet, walk back day by day until we hit one.
        today = fields.Date.context_today(self)
        errors = []
        for delta in range(_MAX_FETCH_ATTEMPTS):
            d = today - timedelta(days=delta)
            fname = f"ProductInformation_{d.isoformat()}.csv.zip"
            url = f"{base}/{fname}"
            _logger.info("IC CSV: trying %s", url)
            try:
                res = requests.get(url, auth=auth, timeout=120)
            except requests.RequestException as exc:
                errors.append(f"{fname}: {exc}")
                continue
            if res.status_code == 200 and res.content:
                _logger.info(
                    "IC CSV: fetched %s (%d bytes)", fname, len(res.content),
                )
                return self._unzip_if_needed(res.content, fname)
            errors.append(
                f"{fname}: HTTP {res.status_code} "
                f"({(res.text or '')[:120]})"
            )
        raise UserError(_(
            "Could not fetch a ProductInformation CSV from IC. "
            "Tried the last %(n)d day(s):\n%(errs)s"
        ) % {'n': _MAX_FETCH_ATTEMPTS, 'errs': '\n'.join(errors)})

    # ── Auto-map BAF ↔ IC seeds ──────────────────────────────────────────
    def _auto_map_seeds(self):
        """Fill ic_seed_sku on BAF templates whose sku maps to an IC row.

        Runs as one UPDATE-FROM in SQL — 1.7M x 500K rows in seconds
        thanks to indexes on the ``n_*`` columns. Only touches
        templates whose seed is currently empty (admin overrides are
        never clobbered).

        Matching uses **normalised** identifiers (whitespace / dots /
        dashes / underscores stripped, upper-cased) so
        ``BMW 61131359287`` finds IC's ``61 13 1 359 287``.

        Preference order (best → worst):
          1. ``n_tow_kod`` — BAF SKU literally is an IC own SKU.
          2. ``n_ic_index`` — the IC index carries the OEM number
             (this is the sweet spot: ``OE MERCEDES``/``OE BMW``
             rows lay their OEM number here in every catalogue).
          3. ``n_article`` — the article-number column, same content
             as tec_doc for most rows.
          4. ``n_tec_doc`` — cross-reference field; collision-prone,
             lowest confidence.

        Within a preference tier, ``OE <brand>`` rows win over
        aftermarket brands — those are IC reselling the original
        manufacturer part, so the match is 1:1 by construction.
        """
        cr = self.env.cr
        # ic_seed_sku is provided by baf_oe_crossref; auto-mapping is
        # a no-op when only ic_intercars is installed.
        from odoo.tools.sql import column_exists
        if not column_exists(cr, 'product_template', 'ic_seed_sku'):
            _logger.info(
                "Auto-map skipped: ic_seed_sku column not present "
                "(baf_oe_crossref not installed).")
            return 0
        # Raw SQL — pending ORM writes must land first, and the ORM
        # cache must be dropped afterwards so records reflect the
        # UPDATE (see invalidate below).
        self.env.flush_all()
        cr.execute(
            r"""
            WITH baf AS (
                SELECT id, regexp_replace(upper(sku), '[\s._\-]', '', 'g') AS n
                FROM product_template
                WHERE sku IS NOT NULL AND sku <> ''
                  AND (ic_seed_sku IS NULL OR ic_seed_sku = '')
            ),
            picked AS (
                SELECT DISTINCT ON (b.id)
                       b.id AS tmpl_id, i.tow_kod
                FROM baf b
                JOIN ic_product_info i
                  ON (
                        i.n_tow_kod  = b.n
                     OR i.n_ic_index = b.n
                     OR i.n_article  = b.n
                     OR i.n_tec_doc  = b.n
                     )
                ORDER BY b.id,
                         CASE
                             WHEN i.n_tow_kod  = b.n THEN 1
                             WHEN i.n_ic_index = b.n THEN 2
                             WHEN i.n_article  = b.n THEN 3
                             WHEN i.n_tec_doc  = b.n THEN 4
                             ELSE 9
                         END,
                         (i.manufacturer NOT LIKE 'OE %%') ASC,
                         i.tow_kod
            )
            UPDATE product_template t
               SET ic_seed_sku = p.tow_kod
              FROM picked p
             WHERE t.id = p.tmpl_id
            """
        )
        mapped = cr.rowcount
        self._auto_create_links()
        # The UPDATE went around the ORM — drop stale cached values.
        self.env.invalidate_all()
        return mapped

    def _auto_create_links(self):
        """Create baf.oe.link rows for high-confidence matches.

        Unlike the seed (one per template), links record EVERY match so
        one OEM can carry several IC alternatives, and one IC SKU can
        serve several OEM templates — the many-to-many the shop needs.

        Only the two collision-safe tiers create links automatically:
        ``n_tow_kod`` (BAF SKU is literally an IC SKU) and
        ``n_ic_index`` (IC's index carries the OEM number, as on all
        ``OE <brand>`` rows). The ``tec_doc`` tier is too collision-
        prone for automatic linking; those matches stay available via
        the resolver's discovery fallback and can be promoted to links
        by hand.

        Existing pairs — including archived ones (rejected matches) —
        are never re-created. No-op when baf_oe_crossref isn't
        installed.
        """
        cr = self.env.cr
        if 'baf.oe.link' not in self.env:
            return 0
        cr.execute(
            r"""
            INSERT INTO baf_oe_link
                   (oem_template_id, ic_sku, source, sequence, active)
            SELECT DISTINCT t.id, i.tow_kod, 'auto', 10, TRUE
            FROM product_template t
            JOIN ic_product_info i
              ON (
                    i.n_tow_kod  = regexp_replace(upper(t.sku), '[\s._\-]', '', 'g')
                 OR i.n_ic_index = regexp_replace(upper(t.sku), '[\s._\-]', '', 'g')
                 )
            WHERE t.sku IS NOT NULL AND t.sku <> ''
              AND NOT EXISTS (
                  SELECT 1 FROM baf_oe_link l
                  WHERE l.oem_template_id = t.id
                    AND l.ic_sku = i.tow_kod
              )
            """
        )
        created = cr.rowcount
        _logger.info("IC auto-map: %d baf.oe.link rows created", created)
        # Also make sure every seeded template has at least its seed
        # as a link (covers seeds picked via the tec_doc tier).
        self.env['baf.oe.link']._populate_from_seeds()
        return created
