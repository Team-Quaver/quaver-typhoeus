"""上游补丁单测（无网络）。pytest 运行：cd vendor/Typhoeus && python -m pytest

回归的就是这个 500（扫码登录状态轮询）：

    File "qqmusic_api/core/response.py", line 75, in snapshot_payload
        cookies[name] = response.cookies[name]
    KeyError: "name='ETK', domain=None, path=None"

根因在 niquests（空值 Cookie 被真值判断吞掉），补丁在 quaver_server.compat。
"""

from __future__ import annotations

import pytest
from niquests.cookies import CookieConflictError, RequestsCookieJar
from qqmusic_api.core.response import snapshot_payload

from quaver_server import compat


class FakeResponse:
    """snapshot_payload 需要的最小响应形状（真的只要这些属性）。"""

    status_code = 200
    url = "https://ssl.ptlogin2.qq.com/ptqrlogin"
    headers = {"content-type": "text/html"}
    content = b"ptuiCB('66','0',...)"

    def __init__(self, jar: RequestsCookieJar) -> None:
        self.cookies = jar
        self.text = self.content.decode()


def jar_with_empty_cookie() -> RequestsCookieJar:
    """未扫码时上游就这么下发：qrsig 有值、ETK 是空串。"""
    jar = RequestsCookieJar()
    jar.set("qrsig", "abc123", domain="qq.com")
    jar.set("ETK", "", domain="qq.com")
    return jar


def test_package_entry_applied_the_patch() -> None:
    """import quaver_server 就该把补丁打上（app.py 第一行就是这个 import）。"""
    assert compat.is_empty_cookie_patched(RequestsCookieJar) is True


def test_apply_is_idempotent() -> None:
    """重复调用不再重复打补丁（第二次应为空列表）。"""
    assert compat.apply_all() == []


def test_empty_value_cookie_is_readable() -> None:
    """空值 Cookie 必须读得出来 —— 名字在 keys() 里、取值就不该抛。"""
    jar = jar_with_empty_cookie()
    assert list(jar.keys()) == ["qrsig", "ETK"]
    assert jar["ETK"] == ""
    assert jar.get("ETK") == ""
    assert jar["qrsig"] == "abc123"


def test_missing_cookie_still_raises() -> None:
    """真不存在还是 KeyError（别把「没这个 Cookie」也吞了）。"""
    jar = jar_with_empty_cookie()
    with pytest.raises(KeyError):
        jar["NOPE"]
    assert jar.get("NOPE") is None


def test_duplicate_name_still_conflicts() -> None:
    """同名跨域仍然是冲突，不能被补丁顺手改成静默取值。"""
    jar = RequestsCookieJar()
    jar.set("ETK", "a", domain="qq.com")
    jar.set("ETK", "b", domain="y.qq.com")
    with pytest.raises(CookieConflictError):
        jar["ETK"]


def test_snapshot_payload_survives_empty_cookie() -> None:
    """复现路径本身：带上空值 ETK 的响应要能构造载荷快照。"""
    payload = snapshot_payload(FakeResponse(jar_with_empty_cookie()))  # type: ignore[arg-type]
    assert payload.cookies == {"qrsig": "abc123", "ETK": ""}
    assert payload.status_code == 200
    assert payload.text.startswith("ptuiCB")


def test_patch_is_skipped_when_upstream_is_fixed() -> None:
    """上游修好之后，探测判据必须返回「没 bug」，补丁自动退化成空操作。"""

    class FixedJar(RequestsCookieJar):
        def _find_no_duplicates(self, name, domain=None, path=None):  # noqa: ANN001
            return ""

    assert compat._empty_value_is_lost(FixedJar) is False
    assert compat.patch_niquests_empty_cookie(FixedJar) is False
    assert compat.is_empty_cookie_patched(FixedJar) is False  # 继承来的标记不算数


def test_probe_is_honest_on_broken_implementation() -> None:
    """反向：真坏实现要被探测认出来（否则补丁静默不打，线上继续 500）。"""

    class BrokenJar(RequestsCookieJar):
        def _find_no_duplicates(self, name, domain=None, path=None):  # noqa: ANN001
            raise KeyError(f"name={name!r}")

    assert compat._empty_value_is_lost(BrokenJar) is True
    assert compat.patch_niquests_empty_cookie(BrokenJar) is True
    jar = BrokenJar()
    jar.set("ETK", "", domain="qq.com")
    assert jar["ETK"] == ""  # 补丁后空值读得出来
    with pytest.raises(KeyError):
        jar["NOPE"]  # 而真不存在仍然是 KeyError
