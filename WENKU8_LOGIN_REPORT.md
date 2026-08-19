# wenku8 登录功能 + 链接修复 实施报告

> 时间：2026-08-18 ｜ 涉及站点：wenku8 ｜ 改动文件：4 个源码 + 文档

## 一、本次做了什么

### 1. 新增 wenku8 登录功能
- `novel/adapters/code/wenku8.py`：`Wenku8Adapter.login(username, password)`
  - POST 到 `https://www.wenku8.net/login.php?do=submit`（表单字段 `username`/`password`/`action=login`）。
  - 登录成功后把服务端下发的会话 Cookie（jieqiUserId / PHPSESSID 等）写入共享 `http_client`（`set_cookies`），并缓存到 `self._session_cookies`。
  - 判定成功：响应 Cookie 含 `jieqiuser*`（如 `jieqiUserInfo`）。
- `novel/core/adapter.py`：`BaseAdapter` 增加默认 `login()` 空实现；`fetch_response()` 在每次请求时合并 `self._session_cookies`，使登录态贯穿中间件链。
- `novel/http/client.py`：`AsyncHTTPClient.set_cookies()`，让登录态也覆盖 `fetch_bytes`（封面 / 插图下载，不经中间件链）。
- **凭据不写死**：从 `CLI --username/--password` 或环境变量 `WENKU8_USERNAME` / `WENKU8_PASSWORD` 读取；新增 `novel sites login <site>` 命令单独验证凭据。

### 2. 修复「链接生成逻辑有误」（核心 bug）
- **根因**：章节目录 URL 写成了固定 `/novel/0/{id}/index.htm`，但 wenku8 实际按 `id // 1000` 分桶存放：`/novel/{id//1000}/{id}/index.htm`。
- 旧代码只对 `id < 1000` 凑巧正确；`id >= 1000` 全部 404、抓不到任何章节。
- 修复：新增 `_build_chapter_index_url(novel_id)` 统一生成正确路径；`base_url` 改为 `https`。
- 实地验证（登录态）：
  - id=1 → `https://www.wenku8.net/novel/0/1/index.htm` ✓（213 章）
  - id=2877 → `https://www.wenku8.net/novel/2/2877/index.htm` ✓（51 章）
  - id=4115 → `https://www.wenku8.net/novel/4/4115/index.htm` ✓（8 章）

### 3. 修复「下架检测误杀全部章节」（顺带发现的关键 bug）
- **根因**：`is_takedown()` 把 `"本文来自 轻小说文库"` 当作下架信号，但这是 wenku8 **每一篇正常章节页的页脚版权声明**。
- 后果：每爬一章都被判为「下架/版权删除」并被跳过 → **所有小说的章节全部被跳过，永远拿不到正文**。这极可能就是「看不到实际数据」的真正主因之一。
- 修复：只保留精确提示语 `因版权问题，文库不再提供该小说的阅读` / `不再提供该小说的阅读`。

### 4. 正文广告噪声过滤
- **问题**：wenku8 章节正文里夹带固定广告/署名段，例如
  `最新最全的日本动漫轻小说 …… 为你一网打尽！`、`本文来自 轻小说文库`、
  `台版 转自 天使动漫论坛`、`轻书架×天使动漫录入组`、`图源：夜殇`、`录入：夜殇`。
- **修复**：`wenku8.py` 新增模块级 `_AD_TEXT_PATTERNS`（8 条正则，覆盖上述文案及残留 URL），
  并在 `get_chapter_content` 解析后调用 `_strip_ad_text(elem)` 遍历 `text`/`tail` 删除命中节点，
  顺带清掉正文里残留的广告 URL 文本。
- 效果：入库正文不再含这些噪声，EPUB 导出也更干净。

## 二、验证结果（真实账号登录 + 真实站点）
- 登录：✅ 成功（用户态 Cookie 已下发）
- 路径分桶：✅ 三个 ID 桶全部正确
- 端到端爬取（CLI `crawl wenku8 2877 --chapters 1-3`，Fetcher + 登录 Cookie 全链路）：
  - 修复前：3 章全部被「下架」跳过
  - 修复后：成功捕获 3 章，字数 30967 / 12513 / 24998
- 回归：`pytest tests/` 12 passed；语法编译通过；`sites info` / `sites login` 命令正常。

## 三、怎么用
```bash
# 设置凭据（推荐环境变量，避免明文进命令历史 / 配置文件）
export WENKU8_USERNAME="你的账号"
export WENKU8_PASSWORD="你的密码"

# 先验证登录
python -m novel sites login wenku8

# 登录后爬取
python -m novel crawl wenku8 2877 --chapters 1-10
python -m novel crawl-all wenku8 --max-novels 50        # 全站（自动续爬，无 --end-id）

# 或用 CLI 参数传入
python -m novel crawl wenku8 wenku8_token --username "x" --password "y"
```
> 说明：wenku8 公开章节本身无需登录即可读；登录主要用于会员/受限内容及保持会话一致性。登录态 Cookie 贯穿章节正文、封面、插图全部请求。

## 四、改动文件清单
| 文件 | 改动 |
|---|---|
| `novel/adapters/code/wenku8.py` | 新增 `login()`、`_build_chapter_index_url()`、`_strip_ad_text()` + 模块级 `_AD_TEXT_PATTERNS`；`base_url`→https；`is_takedown()` 去误判；`__init__` 加 `_session_cookies` |
| `novel/core/adapter.py` | 默认 `login()` 空实现；`fetch_response()` 合并会话 Cookie |
| `novel/http/client.py` | 新增 `set_cookies()` |
| `novel/cli/main.py` | `crawl`/`crawl-all`/`update` 接入登录；新增 `sites login` 命令；凭据解析助手 |
| `USAGE.md` | 补充登录用法、移除已废弃的 `--end-id` 说明 |
