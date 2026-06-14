import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class BafB2BApplyController(http.Controller):

    @http.route(['/b2b/apply'], type='http', auth='public', website=True, sitemap=True)
    def baf_b2b_apply_form(self, **kwargs):
        return request.render('b2b_custom.baf_b2b_apply_page', {
            'values': {},
            'errors': {},
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
            })

        try:
            partner = request.env['res.partner'].sudo().baf_b2b_create_application({
                'company_name': post.get('company_name'),
                'contact_name': post.get('contact_name'),
                'function': post.get('function'),
                'email': post.get('email'),
                'phone': post.get('phone'),
                'street': post.get('street'),
                'zip': post.get('zip'),
                'city': post.get('city'),
                'vat': post.get('vat'),
                'note': post.get('note'),
            })
        except Exception:
            _logger.exception("BAF B2B apply: failed to create partner")
            return request.render('b2b_custom.baf_b2b_apply_page', {
                'values': post,
                'errors': {'__global__': "Antrag konnte nicht gespeichert werden. Bitte erneut versuchen."},
            })

        return request.redirect('/b2b/apply/thanks?ref=%s' % partner.id)

    @http.route(['/b2b/apply/thanks'], type='http', auth='public', website=True)
    def baf_b2b_apply_thanks(self, **kwargs):
        return request.render('b2b_custom.baf_b2b_apply_thanks_page', {})

    @http.route(['/b2b/access-denied'], type='http', auth='user', website=True)
    def baf_b2b_access_denied(self, **kwargs):
        return request.render('b2b_custom.baf_b2b_access_denied_page', {})