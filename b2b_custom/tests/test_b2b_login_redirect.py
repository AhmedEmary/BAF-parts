from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestB2BLoginRedirect(HttpCase):
    """The B2B login form must default its post-login redirect to /b2b, not the
    legacy /bestellsystem page."""

    def test_login_page_defaults_redirect_to_b2b(self):
        response = self.url_open('/web/login')
        response.raise_for_status()
        body = response.text
        self.assertIn('name="redirect"', body, "login form has no redirect field")
        self.assertIn(
            'name="redirect" value="/b2b"', body,
            "B2B login must default the post-login redirect to /b2b")
        self.assertNotIn(
            'name="redirect" value="/bestellsystem"', body,
            "B2B login must not default to the legacy /bestellsystem page")
