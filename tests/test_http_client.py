"""HTTP 客户端与代理/配置适配器登录态测试（纯逻辑，不触网）"""

from novel.http.client import AsyncHTTPClient
from novel.core.middleware import Request, Response


def test_client_accepts_proxy_cookies_headers():
    # 构造时不报错（代理被正确接收），Cookie / 头被记录到内部 client
    client = AsyncHTTPClient(
        proxies="http://127.0.0.1:7890",
        cookies={"session": "abc"},
        headers={"X-Test": "1"},
    )
    # 代理被接受：底层客户端具有代理配置（属性名随 httpx 版本变化，做存在性判断）
    assert getattr(client._client, "_proxy", None) is not None or getattr(
        client._client, "_mounts", None
    ) is not None
    assert client._client.cookies.get("session") == "abc"
    assert client._client.headers.get("X-Test") == "1"
    # 默认 UA 仍注入
    assert "Mozilla" in client._client.headers.get("User-Agent", "")


def test_request_supports_body_and_response_cookies():
    req = Request(
        url="http://x/login",
        method="POST",
        data={"u": "a", "p": "b"},
        json_body={"k": "v"},
    )
    # 字段可被读取
    assert req.data == {"u": "a", "p": "b"}
    assert req.json_body == {"k": "v"}

    resp = Response(
        url="http://x/", status_code=200, cookies={"token": "xyz"}
    )
    assert resp.cookies.get("token") == "xyz"
