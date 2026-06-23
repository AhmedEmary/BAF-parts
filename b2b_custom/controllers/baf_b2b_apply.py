import base64
import json
import logging

from odoo import http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

_logger = logging.getLogger(__name__)

_MAX_TRADE_LICENSE_BYTES = 10 * 1024 * 1024
_ALLOWED_TRADE_LICENSE_EXT = ('.pdf', '.jpg', '.jpeg', '.png')


class BafB2BApplyController(http.Controller):

    def _baf_apply_countries(self):
        return request.env['res.country'].sudo().search([], order='name')

    def _baf_apply_brands(self):
        return request.env['product.brand'].sudo().search([], order='name')

    @http.route(['/b2b/apply'], type='http', auth='public', website=True, sitemap=True)
    def baf_b2b_apply_form(self, **kwargs):
        return request.render('b2b_custom.baf_b2b_apply_page', {
            'values': {},
            'errors': {},
            'countries': self._baf_apply_countries(),
        })

    @http.route(
        ['/b2b/apply/submit'],
        type='http',
        auth='public',
        website=True,
        methods=['POST'],
        csrf=True,
    )
    def baf_b2b_apply_submit(self, **post):
        errors = {}
        required = ('company_name', 'contact_name', 'email')
        for field in required:
            if not (post.get(field) or '').strip():
                errors[field] = "Pflichtfeld"

        email = (post.get('email') or '').strip()
        if email and '@' not in email:
            errors['email'] = "Bitte gültige E-Mail-Adresse eingeben."

        if errors:
            return request.render('b2b_custom.baf_b2b_apply_page', {
                'values': post,
                'errors': errors,
                'countries': self._baf_apply_countries(),
            })

        try:
            country_id = int(post.get('country_id')) if (post.get('country_id') or '').strip().isdigit() else False
            partner = request.env['res.partner'].sudo().baf_b2b_create_application({
                'company_name': post.get('company_name'),
                'contact_name': post.get('contact_name'),
                'function': post.get('function'),
                'email': post.get('email'),
                'phone': post.get('phone'),
                'street': post.get('street'),
                'zip': post.get('zip'),
                'city': post.get('city'),
                'country_id': country_id,
                'vat': post.get('vat'),
                'note': post.get('note'),
            })
        except Exception:
            _logger.exception("BAF B2B apply: failed to create partner")
            return request.render('b2b_custom.baf_b2b_apply_page', {
                'values': post,
                'errors': {'__global__': "Antrag konnte nicht gespeichert werden. Bitte erneut versuchen."},
                'countries': self._baf_apply_countries(),
            })

        return request.redirect('/b2b/apply/thanks?ref=%s' % partner.id)

    @http.route(['/b2b/apply/thanks'], type='http', auth='public', website=True)
    def baf_b2b_apply_thanks(self, **kwargs):
        return request.render('b2b_custom.baf_b2b_apply_thanks_page', {})

    @http.route(['/b2b/register'], type='http', auth='public', website=True, sitemap=True)
    def baf_b2b_register_form(self, **kwargs):
        return request.render('b2b_custom.baf_b2b_register_page', {
            'values': {},
            'errors': {},
            'countries': self._baf_apply_countries(),
            'brands': self._baf_apply_brands(),
        })

    @http.route(
        ['/b2b/register/states'],
        type='http',
        auth='public',
        website=False,
        methods=['GET'],
        csrf=False,
    )
    def baf_b2b_register_states(self, country_id=None, **kwargs):
        try:
            cid = int(country_id or 0)
        except (TypeError, ValueError):
            cid = 0
        states = []
        if cid:
            recs = request.env['res.country.state'].sudo().search(
                [('country_id', '=', cid)], order='name',
            )
            states = [{'id': s.id, 'name': s.name, 'code': s.code} for s in recs]
        return request.make_response(
            json.dumps({'states': states}),
            headers=[('Content-Type', 'application/json')],
        )

    @http.route(
        ['/b2b/register/submit'],
        type='http',
        auth='public',
        website=True,
        methods=['POST'],
        csrf=True,
    )
    def baf_b2b_register_submit(self, **post):
        errors = {}
        required = (
            'company', 'contact', 'email', 'phone', 'vat',
            'street', 'zip', 'city', 'customer_type',
        )
        for field in required:
            if not (post.get(field) or '').strip():
                errors[field] = "Pflichtfeld"

        country_id = False
        raw_country_id = (post.get('country_id') or '').strip()
        if raw_country_id.isdigit():
            country_id = int(raw_country_id)
        if not country_id:
            errors['country_id'] = "Pflichtfeld"

        state_id = False
        raw_state_id = (post.get('state_id') or '').strip()
        if raw_state_id.isdigit():
            state_id = int(raw_state_id)

        raw_brand_ids = request.httprequest.form.getlist('brand_ids')
        brand_ids = [int(b) for b in raw_brand_ids if b and b.isdigit()]
        if brand_ids:
            brand_ids = request.env['product.brand'].sudo().browse(brand_ids).exists().ids
        post['brand_ids'] = brand_ids

        email = (post.get('email') or '').strip()
        if email and '@' not in email:
            errors['email'] = "Bitte gültige E-Mail-Adresse eingeben."

        password = post.get('password') or ''
        password_confirm = post.get('password_confirm') or ''
        if not password:
            errors['password'] = "Pflichtfeld"
        elif len(password) < 8:
            errors['password'] = "Mindestens 8 Zeichen."
        if not password_confirm:
            errors['password_confirm'] = "Pflichtfeld"
        elif password and password != password_confirm:
            errors['password_confirm'] = "Passwörter stimmen nicht überein."

        if email and not errors.get('email'):
            existing = request.env['res.users'].sudo().search(
                [('login', '=', email)], limit=1,
            )
            if existing:
                errors['email'] = "Für diese E-Mail existiert bereits ein Zugang."

        trade_license = request.httprequest.files.get('trade_license')
        if not trade_license or not trade_license.filename:
            errors['trade_license'] = "Pflichtfeld"
        else:
            filename_lower = trade_license.filename.lower()
            if not filename_lower.endswith(_ALLOWED_TRADE_LICENSE_EXT):
                errors['trade_license'] = "Erlaubte Formate: PDF, JPG, JPEG, PNG."

        file_bytes = b''
        if trade_license and not errors.get('trade_license'):
            file_bytes = trade_license.read()
            if len(file_bytes) > _MAX_TRADE_LICENSE_BYTES:
                errors['trade_license'] = "Datei darf max. 10 MB groß sein."

        if errors:
            safe_values = {k: v for k, v in post.items() if k not in ('password', 'password_confirm')}
            return request.render('b2b_custom.baf_b2b_register_page', {
                'values': safe_values,
                'errors': errors,
                'countries': self._baf_apply_countries(),
                'brands': self._baf_apply_brands(),
            })

        note_parts = []
        for label, key in (
            ("Kundentyp", 'customer_type'),
            ("Website", 'website'),
        ):
            value = (post.get(key) or '').strip()
            if value:
                note_parts.append("%s: %s" % (label, value))
        if brand_ids:
            brand_names = request.env['product.brand'].sudo().browse(brand_ids).mapped('name')
            if brand_names:
                note_parts.append("Interessierte Marken: %s" % ", ".join(brand_names))
        message = (post.get('message') or '').strip()
        if message:
            note_parts.append("Nachricht:\n%s" % message)
        note = "\n".join(note_parts)

        partner = None
        try:
            with request.env.cr.savepoint():
                partner = request.env['res.partner'].sudo().baf_b2b_create_application({
                    'company_name': post.get('company'),
                    'contact_name': post.get('contact'),
                    'email': email,
                    'phone': post.get('phone'),
                    'street': post.get('street'),
                    'street2': post.get('street2'),
                    'zip': post.get('zip'),
                    'city': post.get('city'),
                    'country_id': country_id,
                    'state_id': state_id,
                    'brand_ids': brand_ids,
                    'vat': post.get('vat'),
                    'note': note,
                    'password': password,
                })
                website = (post.get('website') or '').strip()
                if website:
                    partner.sudo().write({'website': website})
                if file_bytes:
                    partner.sudo().write({
                        'baf_trade_license': base64.b64encode(file_bytes),
                        'baf_trade_license_filename': trade_license.filename,
                    })
        except (UserError, ValidationError, AccessError) as exc:
            _logger.warning("BAF B2B register: rejected by business rule: %s", exc)
            message = (getattr(exc, 'args', None) and exc.args[0]) or str(exc) \
                or "Antrag konnte nicht gespeichert werden."
            safe_values = {k: v for k, v in post.items() if k not in ('password', 'password_confirm')}
            return request.render('b2b_custom.baf_b2b_register_page', {
                'values': safe_values,
                'errors': {'__global__': message},
                'countries': self._baf_apply_countries(),
                'brands': self._baf_apply_brands(),
            })
        except Exception:
            _logger.exception("BAF B2B register: failed to create partner")
            safe_values = {k: v for k, v in post.items() if k not in ('password', 'password_confirm')}
            return request.render('b2b_custom.baf_b2b_register_page', {
                'values': safe_values,
                'errors': {'__global__': "Antrag konnte nicht gespeichert werden. Bitte erneut versuchen."},
                'countries': self._baf_apply_countries(),
                'brands': self._baf_apply_brands(),
            })

        return request.redirect('/b2b/apply/thanks?ref=%s' % partner.id)

    @http.route(['/b2b/access-denied'], type='http', auth='user', website=True)
    def baf_b2b_access_denied(self, **kwargs):
        return request.render('b2b_custom.baf_b2b_access_denied_page', {})