from odoo import models
from odoo.http import request


B2B_GATED_PREFIXES = (
    '/bestellsystem',
    '/pricefile',
    '/shop',
    '/my',
)

B2B_ALLOWED_EXACT = {
    '/b2b/access-denied',
    '/b2b/apply',
    '/b2b/apply/submit',
    '/b2b/apply/thanks',
    '/web/login',
    '/web/logout',
    '/web/signup',
    '/web/reset_password',
    '/contactus',
}


def _path_is_b2b_gated(path):
    if path in B2B_ALLOWED_EXACT:
        return False
    return any(path == p or path.startswith(p + '/') for p in B2B_GATED_PREFIXES)


class IrHttp(models.AbstractModel):
    _inherit = 'ir.http'

    @classmethod
    def _baf_user_has_b2b_access(cls):
        user = request.env.user
        if not user or user._is_public():
            return False
        if user.has_group('base.group_user'):
            return True
        if user.has_group('b2b_custom.group_b2b_customer'):
            return True
        partner = user.partner_id
        return bool(partner) and partner.baf_b2b_state == 'approved'

    @classmethod
    def _dispatch(cls, endpoint):
        try:
            path = request.httprequest.path or ''
        except Exception:
            path = ''
        if path and _path_is_b2b_gated(path):
            user = request.env.user
            if not user or user._is_public():
                return request.redirect('/b2b/apply')
            if not cls._baf_user_has_b2b_access():
                return request.redirect('/b2b/access-denied')
        return super()._dispatch(endpoint)