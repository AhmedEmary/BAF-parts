"""Two-mode CSV importer for the IC ProductInformation feed.

Mode A — **Upload**: paste the raw ``.csv`` (or its ``.zip``) into the
wizard's file field and click Import. Streams the CSV directly into
Postgres via ``COPY``.

Mode B — **Fetch from IC**: use the ``csv_login`` / ``csv_password``
stored on the active ``ic.backend`` to download today's file from
``https://data.webapi.intercars.eu/customer/<login>/ProductInformation/``.
Same COPY on the way in.

The wizard reports rows inserted + seconds elapsed. Products are
created from the cache afterwards, by hand-picking rows in the
IC Products (cache) list and clicking *Create Products*.
"""

import base64
import io
import logging
import zipfile
from datetime import timedelta

import requests

from odoo import _, fields, models
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
        msg = _(
            "Imported %(rows)d IC products in %(secs).1fs."
        ) % {
            'rows': stats['rows'],
            'secs': stats['seconds'],
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
