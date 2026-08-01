from . import controllers
from . import models


def _post_init_refresh_norm_keys(env):
    """Backfill the normalised search keys for cache rows that were
    imported before this module existed — without this, a database
    with a pre-loaded cache would search an empty index until the
    next CSV import."""
    env['ic.product.info']._baf_refresh_norm_keys()
