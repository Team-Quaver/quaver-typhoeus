"""上游依赖的行为补丁 —— 「不报错、只是悄悄做错事」那一类，必须显式钉住并留一条日志.

目前只有一条：**niquests 的空值 Cookie 判据**。

`niquests.cookies.RequestsCookieJar._find_no_duplicates()` 收尾用的是真值判断：

    if toReturn:
        return toReturn
    raise KeyError(f"name={name!r}, domain={domain!r}, path={path!r}")

于是**值为空字符串的 Cookie 会被当成不存在**并抛 KeyError。QQ 音乐的 `ptqrlogin` 状态接口在
未扫码时就是下发 `ETK=;`（空值），而 QQMusicApi 的 `core/response.py:snapshot_payload()` 恰好是
「先遍历 `cookies.keys()`、再逐个 `cookies[name]` 取值」的写法 —— 名字明明在 keys 里，取值却抛：

    File "qqmusic_api/core/response.py", line 75, in snapshot_payload
        cookies[name] = response.cookies[name]
    KeyError: "name='ETK', domain=None, path=None"

后果是扫码登录（qq/wx/mobile 都会经它）每次状态轮询都 500。RFC 6265 允许空值 Cookie，
按 `is not None` 判空即可 —— 语义与 `_find()`（同类方法，直接 return，不做真值判断）保持一致。

补丁**先探测再打**：上游哪天自己修好了，这里自动退化成空操作（不覆盖、不重复打）。
"""

from __future__ import annotations

import logging

try:
    from niquests.cookies import CookieConflictError
except ImportError:  # 未装 niquests（只 import quaver_server 的场景）不该因此炸掉；补丁那时也不会生效
    CookieConflictError = Exception  # type: ignore[assignment,misc]

logger = logging.getLogger("quaver.compat")

# 探测用 Cookie 名：不可能与真实站点冲突
_PROBE_NAME = "quaver_empty_value_probe"
# 「补丁已打」的标记属性名
_PATCH_FLAG = "_quaver_empty_cookie_patched"


def is_empty_cookie_patched(jar_cls) -> bool:
    """补丁是否已打在**这个类自己**身上。

    用 `__dict__` 而不是 `getattr`：标记会被子类继承，而 `getattr` 会把「父类打过」误判成
    「这个子类也打过了」—— 补丁按类落地，子类要能各打各的。
    """
    return bool(jar_cls.__dict__.get(_PATCH_FLAG))


def _empty_value_is_lost(jar_cls) -> bool:
    """空值 Cookie 现在还取不回来吗？（真值判断的 bug 是否仍然存在）"""
    probe = jar_cls()
    try:
        probe.set(_PROBE_NAME, "", domain="example.com")
        value = jar_cls._find_no_duplicates(probe, _PROBE_NAME)
    except KeyError:
        return True  # 空值被吞掉 → bug 还在
    except Exception:
        return False  # 别的异常：不揽事，保持上游原样
    return value != ""


def patch_niquests_empty_cookie(jar_cls=None) -> bool:
    """把 `_find_no_duplicates` 的 `if toReturn:` 修成 `is not None`。返回是否真的打了补丁。"""
    if jar_cls is None:
        from niquests.cookies import RequestsCookieJar as jar_cls  # noqa: N813

    if is_empty_cookie_patched(jar_cls):
        return False
    if not _empty_value_is_lost(jar_cls):
        return False

    def _find_no_duplicates(self, name, domain=None, path=None):  # noqa: ANN001
        to_return = None
        for cookie in iter(self):
            if cookie.name == name:
                if domain is None or cookie.domain == domain:
                    if path is None or cookie.path == path:
                        if to_return is not None:
                            raise CookieConflictError(f"There are multiple cookies with name, {name!r}")
                        to_return = cookie.value
        if to_return is not None:  # ← 唯一改动：空字符串是合法取值
            return to_return
        raise KeyError(f"name={name!r}, domain={domain!r}, path={path!r}")

    jar_cls._find_no_duplicates = _find_no_duplicates
    setattr(jar_cls, _PATCH_FLAG, True)
    return True


def apply_all() -> list[str]:
    """打上全部补丁，返回本次真正生效的补丁名（测试与日志都用它）。"""
    applied: list[str] = []
    try:
        if patch_niquests_empty_cookie():
            applied.append("niquests:empty-cookie")
    except ImportError:
        logger.warning("未安装 niquests，跳过空值 Cookie 补丁", exc_info=True)
    if applied:
        logger.info("已对上游打补丁: %s", ", ".join(applied))
    return applied
