import json
import logging
import time

from odoo import _, api, fields, modules, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import safe_eval as safe_eval_module
from odoo.tools.safe_eval import safe_eval

_logger = logging.getLogger(__name__)

BATCH_SIZE = 500
# Stay under Odoo's default --limit-time-real-cron (120s). Cron processes a few
# batches per tick; the job resumes on the next tick. Larger budgets risk the
# worker getting SIGTERM'd mid-write, leaving the job stuck in 'processing'.
CRON_TIME_BUDGET_SECONDS = 55
# Stop the manual "Run Now" button ~20s before the default HTTP timeout (120s)
# so the response can flush. Resumes on the next click via last_processed_id.
MANUAL_RUN_BUDGET_SECONDS = 100

# Curated quick-pick fields shown on the "Fields to Update" page. Any other
# stored, writable field can still be set through the "Custom Field" page.
SUPPORTED_FIELDS = [
    'is_storable',
    'taxes_id',
    'supplier_taxes_id',
    'categ_id',
    'website_id',
    'is_published',
    'image_1920',
    'property_account_income_id',
    'property_account_expense_id',
]


class ProductMassUpdate(models.Model):
    _name = 'product.mass.update'
    _description = 'Product Mass Update Job'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    name = fields.Char(default=lambda self: _('Mass Update'), required=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('done', 'Done'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ], default='draft', required=True, tracking=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda s: s.env.company,
        help="Company-dependent fields (income/expense account) are written for this company.",
    )

    # ---- Targeting --------------------------------------------------------
    brand_ids = fields.Many2many('product.brand', string='Brands',
        help="Apply only to products of these brands. Empty = no brand filter.")
    extra_domain = fields.Char(string='Additional Domain',
        help="Optional Odoo domain (e.g. [('list_price','>',100)]). ANDed with the other filters.")
    product_tmpl_ids = fields.Many2many('product.template', string='Specific Products',
        help="If set, only these products are processed (ignores brand/domain).")

    # ---- Field "apply" toggles -------------------------------------------
    apply_is_storable = fields.Boolean()
    apply_taxes_id = fields.Boolean()
    apply_supplier_taxes_id = fields.Boolean()
    apply_categ_id = fields.Boolean()
    apply_website_id = fields.Boolean()
    apply_is_published = fields.Boolean()
    apply_image_1920 = fields.Boolean()
    apply_property_account_income_id = fields.Boolean()
    apply_property_account_expense_id = fields.Boolean()

    # ---- Field values ----------------------------------------------------
    is_storable = fields.Boolean(string='Track Inventory',
        help="When ticked, the products will be marked as Storable (track inventory).")
    taxes_id = fields.Many2many(
        'account.tax', 'product_mass_update_taxes_rel', 'wizard_id', 'tax_id',
        string='Sales Taxes', domain="[('type_tax_use','=','sale')]")
    supplier_taxes_id = fields.Many2many(
        'account.tax', 'product_mass_update_supplier_taxes_rel', 'wizard_id', 'tax_id',
        string='Purchase Taxes', domain="[('type_tax_use','=','purchase')]")
    categ_id = fields.Many2one('product.category', string='Internal Category')
    website_id = fields.Many2one('website', string='Website')
    is_published = fields.Boolean(string='Is Published')
    image_1920 = fields.Image(string='Main Image',
        help="Set this image as the main picture (image_1920) on every selected product. "
             "Shown on the e-commerce product page and the shop listing.")
    property_account_income_id = fields.Many2one(
        'account.account', string='Income Account',
        domain="[('account_type','in',('income','income_other'))]")
    property_account_expense_id = fields.Many2one(
        'account.account', string='Expense Account',
        domain="[('account_type','=','expense')]")

    # ---- Custom field selector ------------------------------------------
    apply_custom_field = fields.Boolean(string='Apply Custom Field')
    custom_field_id = fields.Many2one(
        'ir.model.fields', string='Custom Field',
        domain="[('model','=','product.template'),('store','=',True),('readonly','=',False),"
               "('ttype','in',('char','text','boolean','integer','float','selection',"
               "'many2one','date','datetime'))]")
    custom_field_ttype = fields.Selection(related='custom_field_id.ttype', readonly=True)
    custom_field_relation = fields.Char(related='custom_field_id.relation', readonly=True)

    custom_value_char = fields.Char(string='Value (Text)')
    custom_value_boolean = fields.Boolean(string='Value (Boolean)')
    custom_value_integer = fields.Integer(string='Value (Integer)')
    custom_value_float = fields.Float(string='Value (Float)')
    custom_value_date = fields.Date(string='Value (Date)')
    custom_value_datetime = fields.Datetime(string='Value (Date Time)')
    custom_value_reference = fields.Reference(
        selection='_get_custom_value_reference_models', string='Value (Record)')
    custom_value_selection_key = fields.Char(
        string='Selection Key',
        help="Technical key of the selection value (see help on the chosen field for valid keys).")

    # ---- Progress / state ------------------------------------------------
    total_count = fields.Integer(readonly=True)
    processed_count = fields.Integer(readonly=True)
    progress = fields.Float(compute='_compute_progress', string='Progress (%)')
    last_processed_id = fields.Integer(readonly=True, default=0)
    start_time = fields.Datetime(readonly=True)
    end_time = fields.Datetime(readonly=True)
    error_log = fields.Text(readonly=True)
    vals_json = fields.Text(readonly=True,
        help="Frozen field values written at launch time (JSON).")
    domain_json = fields.Text(readonly=True,
        help="Frozen domain computed at launch time (JSON).")

    # ---- Dry-run preview --------------------------------------------------
    dry_run_done = fields.Boolean(readonly=True)
    dry_run_count = fields.Integer(readonly=True, string='Matched Products')
    dry_run_summary = fields.Html(readonly=True, string='Changes Preview', sanitize=False)
    dry_run_sample_ids = fields.Many2many(
        'product.template', 'product_mass_update_sample_rel', 'wizard_id', 'product_id',
        string='Sample Products', readonly=True)

    # ---------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------
    @api.model
    def _get_custom_value_reference_models(self):
        Field = self.env['ir.model.fields'].sudo()
        relations = Field.search([
            ('model', '=', 'product.template'),
            ('ttype', '=', 'many2one'),
            ('store', '=', True),
        ]).mapped('relation')
        models_seen = set()
        result = []
        for rel in relations:
            if rel and rel not in models_seen and rel in self.env:
                models_seen.add(rel)
                result.append((rel, self.env[rel]._description or rel))
        return sorted(result, key=lambda x: x[1])

    @api.depends('total_count', 'processed_count')
    def _compute_progress(self):
        for rec in self:
            rec.progress = (100.0 * rec.processed_count / rec.total_count) if rec.total_count else 0.0

    # ---------------------------------------------------------------------
    # Validation + launch
    # ---------------------------------------------------------------------
    def _collect_vals(self):
        """Build the dict of {field_name: value} that will be written to each product."""
        self.ensure_one()
        vals = {}
        for fname in SUPPORTED_FIELDS:
            if not self['apply_' + fname]:
                continue
            f = self._fields[fname]
            value = self[fname]
            if f.type == 'many2one':
                vals[fname] = value.id or False
            elif f.type == 'many2many':
                vals[fname] = [(6, 0, value.ids)]
            elif f.type in ('binary', 'image'):
                # vals_json stores everything as JSON; bytes aren't JSON-serializable.
                # Image fields hold base64 bytes — decode to str for storage, write()
                # accepts either form when the batch runs.
                if not value:
                    raise ValidationError(_("Upload an image before launching, or untick 'Main Image'."))
                vals[fname] = value.decode('ascii') if isinstance(value, bytes) else value
            else:
                vals[fname] = value

        if self.apply_custom_field:
            if not self.custom_field_id:
                raise ValidationError(_("Pick a custom field or untick 'Apply Custom Field'."))
            fname = self.custom_field_id.name
            if fname in vals:
                raise ValidationError(_("Field %s is already set above; remove the duplicate.") % fname)
            ttype = self.custom_field_id.ttype
            if ttype in ('char', 'text'):
                vals[fname] = self.custom_value_char or False
            elif ttype == 'boolean':
                vals[fname] = self.custom_value_boolean
            elif ttype == 'integer':
                vals[fname] = self.custom_value_integer
            elif ttype == 'float':
                vals[fname] = self.custom_value_float
            elif ttype == 'date':
                vals[fname] = self.custom_value_date or False
            elif ttype == 'datetime':
                vals[fname] = self.custom_value_datetime or False
            elif ttype == 'selection':
                if not self.custom_value_selection_key:
                    raise ValidationError(_("Provide a selection key for field %s.") % fname)
                # Validate the key is one of the field's options
                target = self.env['product.template']._fields.get(fname)
                valid_keys = [k for k, _label in (target.selection or [])] if target else []
                if valid_keys and self.custom_value_selection_key not in valid_keys:
                    raise ValidationError(_(
                        "Selection key %(key)s is not valid for %(field)s. Valid keys: %(keys)s",
                        key=self.custom_value_selection_key, field=fname,
                        keys=", ".join(valid_keys)))
                vals[fname] = self.custom_value_selection_key
            elif ttype == 'many2one':
                ref = self.custom_value_reference
                if ref and ref._name != self.custom_field_id.relation:
                    raise ValidationError(_(
                        "Picked record is on model %(picked)s but field %(field)s expects %(expected)s.",
                        picked=ref._name, field=fname, expected=self.custom_field_id.relation))
                vals[fname] = ref.id if ref else False
            else:
                raise ValidationError(_("Field type %s is not supported for mass update.") % ttype)

        if not vals:
            raise ValidationError(_("Tick at least one field to update."))
        return vals

    def _build_domain(self):
        self.ensure_one()
        if self.product_tmpl_ids:
            return [('id', 'in', self.product_tmpl_ids.ids)]
        domain = []
        if self.brand_ids:
            domain.append(('brand', 'in', self.brand_ids.ids))
        if self.extra_domain:
            try:
                extra = safe_eval(self.extra_domain, {
                    'datetime': safe_eval_module.datetime,
                    'time': safe_eval_module.time,
                })
            except Exception as e:
                raise ValidationError(_("Invalid additional domain: %s") % e)
            if not isinstance(extra, list):
                raise ValidationError(_("Additional domain must be a list."))
            domain += extra
        return domain

    def action_launch(self):
        self.ensure_one()
        if self.state not in ('draft', 'failed', 'cancelled'):
            raise UserError(_("Job is already %s.") % self.state)
        vals = self._collect_vals()
        domain = self._build_domain()
        # Snapshot domain + vals so view changes after launch don't affect the run.
        self.write({
            'state': 'pending',
            'vals_json': json.dumps(vals),
            'domain_json': json.dumps(domain),
            'processed_count': 0,
            'last_processed_id': 0,
            'error_log': False,
            'start_time': False,
            'end_time': False,
            'total_count': 0,
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Mass update queued"),
                'message': _("The job will start within a minute. You will receive an email when it finishes."),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

    def action_dry_run(self):
        """Validate inputs, count matching products, render a preview.
        Writes nothing to product.template."""
        self.ensure_one()
        if self.state not in ('draft', 'failed', 'cancelled'):
            raise UserError(_("Dry run is only available before launching."))
        vals = self._collect_vals()
        domain = self._build_domain()
        Product = self.env['product.template'].with_context(active_test=False)
        count = Product.search_count(domain)
        sample = Product.search(domain, limit=10)
        summary_html = self._render_dry_run_summary(vals, count, sample)
        self.write({
            'dry_run_done': True,
            'dry_run_count': count,
            'dry_run_summary': summary_html,
            'dry_run_sample_ids': [(6, 0, sample.ids)],
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Dry run complete"),
                'message': _("%d products would be updated. Review the preview, then click Launch.") % count,
                'type': 'info',
                'sticky': False,
            },
        }

    def _render_dry_run_summary(self, vals, count, sample):
        """Build a small HTML report of what would happen, with current vs new
        values for the first sample product (for sanity check)."""
        self.ensure_one()
        Product = self.env['product.template']
        rows = []
        for fname, new_value in vals.items():
            field = Product._fields.get(fname)
            if not field:
                continue
            label = field.string or fname
            new_label = self._format_value_for_display(field, new_value)
            current_label = ''
            if sample:
                first = sample[0].with_company(self.company_id or self.env.company)
                current_label = self._format_value_for_display(field, first[fname], record_value=True)
            rows.append(
                f"<tr><td style='padding:4px 12px;border-bottom:1px solid #eee;'><b>{label}</b></td>"
                f"<td style='padding:4px 12px;border-bottom:1px solid #eee;color:#888;'>{current_label}</td>"
                f"<td style='padding:4px 12px;border-bottom:1px solid #eee;'>&rarr;</td>"
                f"<td style='padding:4px 12px;border-bottom:1px solid #eee;color:#1f7a1f;'><b>{new_label}</b></td></tr>"
            )
        sample_names = ', '.join(sample.mapped('display_name')[:5]) or _('(none)')
        return (
            f"<div><p><b>{count}</b> products match. "
            f"<i>Showing current value for the first one as a sanity check; new value applies to all.</i></p>"
            f"<table style='border-collapse:collapse;margin-top:8px;'>"
            f"<thead><tr style='background:#f5f5f5;'>"
            f"<th style='padding:4px 12px;text-align:left;'>Field</th>"
            f"<th style='padding:4px 12px;text-align:left;'>Current (1st sample)</th>"
            f"<th></th>"
            f"<th style='padding:4px 12px;text-align:left;'>New value</th>"
            f"</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
            f"<p style='margin-top:12px;color:#666;'>Sample: {sample_names}</p></div>"
        )

    def _format_value_for_display(self, field, value, record_value=False):
        """Render a write-vals value (or a record's current value) as a string."""
        if value in (False, None, ''):
            return _('(empty)')
        if field.type == 'many2one':
            if record_value:
                return value.display_name if value else _('(empty)')
            rec = self.env[field.comodel_name].browse(value).exists()
            return rec.display_name if rec else str(value)
        if field.type == 'many2many':
            if record_value:
                return ', '.join(value.mapped('display_name')) or _('(empty)')
            ids = value
            if isinstance(value, list) and value and isinstance(value[0], (list, tuple)):
                # write-style command, e.g. [(6, 0, [ids])]
                ids = value[0][2] if len(value[0]) > 2 else []
            recs = self.env[field.comodel_name].browse(ids).exists()
            return ', '.join(recs.mapped('display_name')) or _('(empty)')
        if field.type == 'selection':
            sel = dict(field._description_selection(self.env))
            return sel.get(value, value)
        if field.type == 'boolean':
            return _('Yes') if value else _('No')
        if field.type in ('binary', 'image'):
            return _('(image set)') if value else _('(no image)')
        return str(value)

    @api.onchange(
        'apply_is_storable', 'is_storable',
        'apply_taxes_id', 'taxes_id',
        'apply_supplier_taxes_id', 'supplier_taxes_id',
        'apply_categ_id', 'categ_id',
        'apply_website_id', 'website_id',
        'apply_is_published', 'is_published',
        'apply_image_1920', 'image_1920',
        'apply_property_account_income_id', 'property_account_income_id',
        'apply_property_account_expense_id', 'property_account_expense_id',
        'apply_custom_field', 'custom_field_id',
        'custom_value_char', 'custom_value_boolean', 'custom_value_integer',
        'custom_value_float', 'custom_value_date', 'custom_value_datetime',
        'custom_value_selection_key', 'custom_value_reference',
        'brand_ids', 'extra_domain', 'product_tmpl_ids', 'company_id',
    )
    def _onchange_invalidate_dry_run(self):
        if self.dry_run_done:
            self.dry_run_done = False
            self.dry_run_count = 0
            self.dry_run_summary = False
            self.dry_run_sample_ids = [(5, 0, 0)]

    def action_cancel(self):
        for rec in self:
            if rec.state in ('done', 'failed', 'cancelled'):
                continue
            rec.state = 'cancelled'
            rec.end_time = fields.Datetime.now()

    def action_process_now(self):
        """Run the job synchronously, batch after batch, until it finishes or
        we approach the HTTP timeout. Safe to click again to resume — the
        cursor (last_processed_id) means we never reprocess the same rows.
        Posts one chatter entry per click summarising what happened."""
        self.ensure_one()
        if self.state not in ('pending', 'processing'):
            raise UserError(_(
                "Job must be Pending or Processing to run now. Current state: %s"
            ) % self.state)

        in_test = modules.module.current_test
        started = time.monotonic()
        deadline = started + MANUAL_RUN_BUDGET_SECONDS
        batches = 0
        more = True

        while more and time.monotonic() < deadline:
            try:
                more = self._process_one_batch()
            except Exception as e:
                _logger.exception("Mass update job %s failed (manual run)", self.id)
                if not in_test:
                    self.env.cr.rollback()
                fresh = self.browse(self.id)
                fresh.write({
                    'state': 'failed',
                    'end_time': fields.Datetime.now(),
                    'error_log': str(e),
                })
                fresh.message_post(body=_(
                    "Manual run failed after %(n)d batch(es): %(e)s",
                    n=batches, e=e,
                ))
                fresh._send_completion_email()
                if not in_test:
                    self.env.cr.commit()
                return {'type': 'ir.actions.client', 'tag': 'soft_reload'}

            batches += 1
            # Commit each batch so progress survives a timeout/cancel.
            if not in_test:
                self.env.cr.commit()

        total_elapsed = time.monotonic() - started
        if more:
            self.message_post(body=_(
                "Manual run paused at %(p)d / %(total)d products after %(b)d batch(es) "
                "in %(t).0fs (time budget reached). Click <b>Run Now</b> again to continue, "
                "or wait for the cron.",
                p=self.processed_count, total=self.total_count, b=batches, t=total_elapsed,
            ))
        else:
            self.message_post(body=_(
                "Manual run finished — %(p)d / %(total)d products in %(b)d batch(es), %(t).1fs total.",
                p=self.processed_count, total=self.total_count, b=batches, t=total_elapsed,
            ))
        return {'type': 'ir.actions.client', 'tag': 'soft_reload'}

    def action_open_products(self):
        self.ensure_one()
        domain = json.loads(self.domain_json) if self.domain_json else self._build_domain()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Targeted Products"),
            'res_model': 'product.template',
            'view_mode': 'list,form',
            'domain': domain,
        }

    # ---------------------------------------------------------------------
    # Worker
    # ---------------------------------------------------------------------
    @api.model
    def _cron_process_jobs(self):
        """Cron entry point. Picks up pending/processing jobs and runs them
        within a time budget so the worker can be released for other crons.
        Posts one chatter entry per processed job per tick (or on failure)."""
        in_test = modules.module.current_test
        tick_start = time.monotonic()
        deadline = tick_start + CRON_TIME_BUDGET_SECONDS

        # Per-job accumulators so we post a single summary at the end of the tick.
        # Keyed by job.id → {'batches': int, 'finished': bool, 'started_processed': int}
        job_stats = {}

        while time.monotonic() < deadline:
            job = self.search([('state', 'in', ('pending', 'processing'))],
                              order='create_date asc', limit=1)
            if not job:
                break

            stats = job_stats.setdefault(job.id, {
                'batches': 0,
                'finished': False,
                'started_processed': job.processed_count,
            })
            try:
                more = job._process_one_batch()
            except Exception as e:
                _logger.exception("Mass update job %s failed", job.id)
                if not in_test:
                    self.env.cr.rollback()
                job_fresh = self.browse(job.id)
                job_fresh.write({
                    'state': 'failed',
                    'end_time': fields.Datetime.now(),
                    'error_log': str(e),
                })
                job_fresh.message_post(body=_(
                    "Cron run failed after %(n)d batch(es): %(e)s",
                    n=stats['batches'], e=e,
                ))
                job_fresh._send_completion_email()
                if not in_test:
                    self.env.cr.commit()
                # Drop this job's pending summary — failure message replaces it.
                job_stats.pop(job.id, None)
                continue

            stats['batches'] += 1
            if not more:
                stats['finished'] = True
            if not in_test:
                self.env.cr.commit()

        # End-of-tick summary, one chatter entry per job touched this tick.
        for job_id, stats in job_stats.items():
            job = self.browse(job_id)
            if not job.exists():
                continue
            written = job.processed_count - stats['started_processed']
            if stats['finished']:
                job.message_post(body=_(
                    "Cron processed %(n)d batch(es) (%(w)d products) — job finished.",
                    n=stats['batches'], w=written,
                ))
            else:
                job.message_post(body=_(
                    "Cron processed %(n)d batch(es) (%(w)d products); resumes next tick. "
                    "Progress: %(p)d / %(total)d.",
                    n=stats['batches'], w=written,
                    p=job.processed_count, total=job.total_count,
                ))

    def _process_one_batch(self):
        """Process a single batch. Returns True if more batches remain, False if done."""
        self.ensure_one()
        if self.state == 'pending':
            domain = json.loads(self.domain_json or '[]')
            total = self.env['product.template'].with_context(active_test=False).search_count(domain)
            self.write({
                'state': 'processing',
                'start_time': fields.Datetime.now(),
                'total_count': total,
            })
            if total == 0:
                self.write({'state': 'done', 'end_time': fields.Datetime.now()})
                self._send_completion_email()
                return False

        domain = json.loads(self.domain_json or '[]')
        cursor_domain = domain + [('id', '>', self.last_processed_id)]
        Product = self.env['product.template'].with_context(active_test=False)
        products = Product.search(cursor_domain, order='id asc', limit=BATCH_SIZE)
        if not products:
            self.write({'state': 'done', 'end_time': fields.Datetime.now()})
            self._send_completion_email()
            return False

        vals = json.loads(self.vals_json or '{}')
        # Re-shape m2m commands (JSON turns tuples into lists; Odoo accepts both).
        company = self.company_id or self.env.company
        products.with_company(company).with_context(
            tracking_disable=True,
            mail_create_nolog=True,
            mail_notrack=True,
        ).write(vals)

        self.write({
            'last_processed_id': products[-1].id,
            'processed_count': self.processed_count + len(products),
        })
        return True

    # ---------------------------------------------------------------------
    # Notifications
    # ---------------------------------------------------------------------
    def _send_completion_email(self):
        self.ensure_one()
        template = self.env.ref(
            'b2b_custom.mail_template_product_mass_update_done',
            raise_if_not_found=False,
        )
        if not template:
            return
        user = self.create_uid
        if not user or not user.email:
            return
        try:
            template.with_context(
                recipient_email=user.email,
                recipient_name=user.name,
            ).send_mail(self.id, force_send=True, email_values={'email_to': user.email})
        except Exception:
            _logger.exception("Failed to send mass-update completion email for job %s", self.id)


class ProductTemplateMassUpdateAction(models.Model):
    _inherit = 'product.template'

    def action_open_mass_update_wizard(self):
        """Server action target: open the wizard pre-populated with selected products."""
        wizard = self.env['product.mass.update'].create({
            'name': _("Mass Update — %d products selected") % len(self),
            'product_tmpl_ids': [(6, 0, self.ids)],
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _("Mass Update"),
            'res_model': 'product.mass.update',
            'res_id': wizard.id,
            'view_mode': 'form',
            'target': 'new',
        }
