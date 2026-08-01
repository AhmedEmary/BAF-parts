{
    'name': "Inter Cars Website Catalog Search",
    'summary': "Customer-facing search over the Inter Cars catalog "
               "cache with on-demand product materialisation.",
    'category': 'Website/eCommerce',
    'version': '1.0',
    'depends': [
        'ic_intercars',
        'website_sale',
    ],
    'installable': True,
    'post_init_hook': '_post_init_refresh_norm_keys',
    'data': [
        'views/templates.xml',
        'data/website_menu.xml',
    ],
    'author': 'Ahmed Elamery',
    'license': 'LGPL-3',
}
