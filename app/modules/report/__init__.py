from flask import Blueprint

report_admin_bp = Blueprint(
    'report_admin',
    __name__,
    template_folder='templates',
    url_prefix='/admin',
)

from . import routes  # noqa: E402, F401
from .web_routes import report_web_bp  # noqa: E402

__all__ = ['report_admin_bp', 'report_web_bp']
