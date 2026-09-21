from lxml import etree

from odoo.tests.common import tagged

from .common import LexcomCommon

MANDATORY = {"commands-available", "order-submit", "order-append"}


@tagged("post_install", "-at_install")
class TestCommandsAvailable(LexcomCommon):

    def _dispatch(self, xml):
        root = etree.fromstring(xml)
        element, outcome, message, order = self.env["lexcom.protocol"]._dispatch(
            "commands-available", root, self.company
        )
        return element, outcome

    def test_advertises_version_name_and_commands(self):
        element, outcome = self._dispatch(b"<commands-available/>")
        self.assertEqual(outcome, "ok")
        self.assertEqual(element.tag, "commands-available-response")
        self.assertEqual(element.get("version"), "3.1")
        self.assertTrue(element.get("dms-name"))
        advertised = {el.text for el in element.findall("command")}
        self.assertEqual(advertised, MANDATORY)

    def test_request_version_is_ignored(self):
        """The spec says the server MUST ignore this attribute.

        It is the discovery call, so refusing it on version grounds would make
        version discovery impossible.
        """
        element, outcome = self._dispatch(
            b'<commands-available version="9.9"/>'
        )
        self.assertEqual(outcome, "ok")
        self.assertEqual(element.get("version"), "3.1")

    def test_stock_information_is_not_advertised(self):
        """Deferred by decision; advertising it would invite requests."""
        element, _outcome = self._dispatch(b"<commands-available/>")
        advertised = {el.text for el in element.findall("command")}
        self.assertNotIn("stock-information", advertised)
        self.assertNotIn("order-list", advertised)
