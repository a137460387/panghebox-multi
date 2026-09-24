"""胖盒云电脑 多账号切换 + 自动签到工具。

一个非官方的本地工具,通过复用客户端自身的登录态实现账号切换,
并提供每日签到的命令行自动化。

⚠️ 本工具会在本地明文保存登录凭据(token / accessKey / 手机号),
   请勿将 accounts/ 目录分享或提交到版本库。
"""

from __future__ import annotations

__version__ = "0.2.0"
__all__ = [
    "__version__",
    "Account",
    "AccountStore",
    "AppContext",
    "PangHeClient",
]
