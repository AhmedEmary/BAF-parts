"""Rename the Alzura partner tag from 'Alzura Buyer' to 'Alzura / Tyre24'.

The import used to tag buyers with its own 'Alzura Buyer' category while the
customer team already classifies these partners with the pre-existing
'Alzura / Tyre24' tag. Move every partner from the old tag to the existing
one (creating it if this DB never had it) and drop the old tag, so the list
view shows a single consistent tag.
"""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Category = env["res.partner.category"]

    old_tags = Category.search([("name", "=", "Alzura Buyer")])
    if not old_tags:
        return

    new_tag = Category.search([("name", "=", "Alzura / Tyre24")], limit=1)
    if not new_tag:
        new_tag = Category.create({"name": "Alzura / Tyre24"})

    partners = (
        env["res.partner"]
        .with_context(active_test=False)
        .search([("category_id", "in", old_tags.ids)])
    )
    if partners:
        partners.write(
            {"category_id": [(3, tag.id) for tag in old_tags] + [(4, new_tag.id)]}
        )
    old_tags.unlink()
