"""ConfigAdapter 登录态与 Cookie 维持测试（不触网）"""

import asyncio

from novel.adapters.config_adapter import ConfigAdapter
from novel.core.middleware import Request, Response


class FakeClient:
    """记录请求，模拟登录响应携带 Set-Cookie。"""

    def __init__(self):
        self.calls = []

    async def fetch(self, request: Request) -> Response:
        self.calls.append(request)
        if request.url.endswith("/login.php"):
            return Response(
                url=request.url,
                status_code=200,
                cookies={"PHPSESSID": "sess123"},
            )
        # 普通页面请求：返回带 Cookie 回显的文本，便于断言
        body = f"cookies={request.cookies}".encode("utf-8")
        return Response(
            url=request.url,
            status_code=200,
            raw_body=body,
            text=body.decode("utf-8"),
        )


def test_login_performs_and_stores_cookies():
    cfg = {
        "site_name": "demo",
        "base_url": "http://demo.local",
        "encoding": "utf-8",
        "login": {
            "url": "/login.php",
            "method": "POST",
            "form": {"user": "u", "pass": "p"},
        },
    }
    adapter = ConfigAdapter(cfg)
    fake = FakeClient()
    adapter._http_client = fake

    async def run():
        await adapter.ensure_authenticated()
        # 第二次 fetch_text 应携带登录 Cookie
        text = await adapter.fetch_text("http://demo.local/book/1")
        return text

    text = asyncio.run(run())

    # 登录请求被发出
    assert any(c.url.endswith("/login.php") for c in fake.calls)
    # 登录后 Cookie 已保存并在后续请求中回传
    assert adapter._authed is True
    assert adapter._cookies.get("PHPSESSID") == "sess123"
    assert "sess123" in text


def test_supports_login_flag():
    adapter_no_login = ConfigAdapter({"site_name": "a", "base_url": "http://a"})
    assert adapter_no_login.supports_login() is False

    adapter_login = ConfigAdapter(
        {"site_name": "b", "base_url": "http://b", "login": {"url": "/l"}}
    )
    assert adapter_login.supports_login() is True
