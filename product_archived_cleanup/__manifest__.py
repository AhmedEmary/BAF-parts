{
    'name': "Product Archived Cleanup",
    'summary': "Background scheduled action to permanently delete deletable archived products",
    'version': '1.0',
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