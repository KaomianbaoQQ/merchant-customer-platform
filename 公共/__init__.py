"""公共基础模块：数据库连接等，供各业务模块复用。"""

# Dify、后端、MySQL 都跑在本机。如果系统开了全局 HTTP 代理（Clash 等），
# requests 会把发往 localhost 的请求也交给代理，代理回 502，
# 现象和「Dify 挂掉了」一模一样。这里把本机地址加进 NO_PROXY，
# 任何入口 import 公共.* 时自动生效，且不改动原有代理设置。
import os as _os


def _bypass_proxy_for_local():
    _hosts = ("localhost", "127.0.0.1", "::1")
    for _var in ("NO_PROXY", "no_proxy"):
        _parts = [p.strip() for p in _os.environ.get(_var, "").split(",") if p.strip()]
        for _h in _hosts:
            if _h not in _parts:
                _parts.append(_h)
        _os.environ[_var] = ",".join(_parts)


_bypass_proxy_for_local()
del _bypass_proxy_for_local, _os
