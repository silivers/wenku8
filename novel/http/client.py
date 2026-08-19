"""异步 HTTP 客户端，基于 httpx"""

from __future__ import annotations

import httpx

from novel.core.middleware import Request, Response


class AsyncHTTPClient:
    """封装 httpx.AsyncClient，提供统一的请求接口。"""

    def __init__(
        self,
        timeout_connect: float = 10,
        timeout_read: float = 30,
        follow_redirects: bool = True,
        proxies: str | dict | None = None,
        cookies: dict | None = None,
        headers: dict | None = None,
    ):
        merged_headers = {
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36",
        }
        if headers:
            merged_headers.update(headers)

        client_kwargs: dict = dict(
            timeout=httpx.Timeout(timeout_read, connect=timeout_connect),
            follow_redirects=follow_redirects,
            headers=merged_headers,
        )
        if cookies:
            client_kwargs["cookies"] = cookies
        if proxies:
            # httpx 0.28+: 字符串用 proxy=，字典（按 scheme）用 mounts=
            if isinstance(proxies, str):
                client_kwargs["proxy"] = proxies
            elif isinstance(proxies, dict):
                from httpx import Proxy

                client_kwargs["mounts"] = {
                    scheme: Proxy(url) for scheme, url in proxies.items()
                }

        self._client = httpx.AsyncClient(**client_kwargs)

    def set_cookies(self, cookies: dict) -> None:
        """写入会话 Cookie（如登录后下发的 jieqiUserId / PHPSESSID）。

        写入后，本客户端发起的所有请求（含 fetch 与 fetch_bytes）都会自动携带，
        从而登录态贯穿章节正文、封面与插图下载。
        """
        if cookies:
            self._client.cookies.update(cookies)

    async def fetch(self, request: Request) -> Response:
        """执行 HTTP 请求，返回 Response 对象。"""
        resp = await self._client.request(
            method=request.method,
            url=request.url,
            headers=request.headers or None,
            params=request.params or None,
            cookies=request.cookies or None,
            data=request.data,
            json=request.json_body,
            timeout=request.timeout,
        )

        # 从 Content-Type 头提取 charset
        charset = None
        content_type = resp.headers.get("content-type", "")
        if "charset=" in content_type:
            charset = content_type.split("charset=")[-1].strip().strip('"').strip("'")

        return Response(
            url=str(resp.url),
            status_code=resp.status_code,
            headers=dict(resp.headers),
            raw_body=resp.content,
            text=resp.text,
            encoding=str(resp.encoding) if resp.encoding else "utf-8",
            charset=charset,
            cookies=dict(resp.cookies),
            request=request,
        )

    async def fetch_bytes(self, url: str, headers: dict | None = None) -> bytes:
        """直接下载二进制内容（如图片）。"""
        resp = await self._client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.content

    async def close(self) -> None:
        await self._client.aclose()
