"""注册全部 API / 页面蓝图。"""
from .auth import bp as auth_bp
from .consult import bp as consult_bp
from .after_sales import bp as after_sales_bp
from .orders import bp as orders_bp
from .addresses import bp as addresses_bp
from .returns import bp as returns_bp
from .logistics import bp as logistics_bp
from .health import bp as health_bp
from .pages import bp as pages_bp
from .products import bp as products_bp
from .chat import bp as merchant_chat_bp
from .merchant import bp as merchant_admin_bp
from .platform import bp as platform_bp
from .reviews import bp as reviews_bp
from .analytics import bp as analytics_bp
from .cart import bp as cart_bp
from .upload import bp as upload_bp

ALL_BLUEPRINTS = (
    pages_bp,
    health_bp,
    auth_bp,
    consult_bp,
    after_sales_bp,
    orders_bp,
    addresses_bp,
    returns_bp,
    logistics_bp,
    products_bp,
    merchant_chat_bp,
    merchant_admin_bp,
    platform_bp,
    reviews_bp,
    analytics_bp,
    cart_bp,
    upload_bp,
)


def register_routes(app):
    """把所有蓝图注册到 Flask app。"""
    for bp in ALL_BLUEPRINTS:
        app.register_blueprint(bp)
