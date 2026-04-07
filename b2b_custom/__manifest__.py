{
    'name': "B2B custom",
    'summary': "This is a custom sale module for B2B companies",
    'version': '1.0',
    'category': 'Sales/Sales',
    'depends': ['general_system_custom', 'website_sale', 'mail'],
    'installable': True,
    'data': [
        'security/ir.model.access.csv',
        'views/product_template_view.xml',
        'views/website_list_to_part_template.xml',
        'views/website_sale_cart_view.xml',
        'views/website_backorders_template.xml',
        'views/checkout_template.xml',
        'views/sale_order.xml',
        'views/mass_product_import_view.xml'
    ],
    'author': 'Ahmed Elamery',
    'license': 'LGPL-3',
}
