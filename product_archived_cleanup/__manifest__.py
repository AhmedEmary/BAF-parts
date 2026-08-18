{
    'name': "Product Archived Cleanup",
    'summary': "Background scheduled action to permanently delete deletable archived products",
    'version': '1.0',
    # Explicit description: without it Odoo feeds README.md to a
    # reStructuredText parser, which trips on the Markdown backticks.
    'description': """
Product Archived Cleanup
========================

Cron job that permanently deletes archived product templates in chunks,
bounded by a batch size and a wall-clock time budget. Both are system
parameters, read on every tick. See README.md for details.
""",
    'category': 'Inventory/Inventory',
    'depends': ['product'],
    'installable': True,
    'data': [
        'data/ir_config_parameter.xml',
        'data/ir_cron.xml',
    ],
    'author': 'Ahmed Elamery',
    'license': 'LGPL-3',
}