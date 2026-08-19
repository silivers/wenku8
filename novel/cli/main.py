"""CLI 主入口"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import click
from loguru import logger

from novel.config import AppConfig


def setup_logging(config: AppConfig) -> None:
    """配置日志。"""
    logger.remove()
    level = config.logging.level.upper()

    # 控制台输出
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}",
    )

    # 文件输出
    if config.logging.file:
        log_path = Path(config.logging.file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(log_path),
            level=level,
            format="{time:YYYY-MM-DD HH:mm:ss} | {level:<7} | {message}",
            rotation="10 MB",
            retention="30 days",
        )


def create_engine_from_config(config: AppConfig, site: str | None = None):
    """根据配置创建引擎所需的所有组件。"""
    from novel.adapters.registry import AdapterRegistry
    from novel.http.client import AsyncHTTPClient
    from novel.storage.sqlite_backend import SQLiteBackend

    # 存储后端
    if config.storage.backend == "sqlite":
        storage = SQLiteBackend(config.storage.sqlite.path)
    else:
        raise ValueError(f"不支持的存储后端: {config.storage.backend}")

    # HTTP 客户端（支持代理 / Cookie / 自定义头）
    http_client = AsyncHTTPClient(
        timeout_connect=config.http.timeout_connect,
        timeout_read=config.http.timeout_read,
        follow_redirects=config.http.follow_redirects,
        proxies=config.http.proxy or None,
    )

    # 适配器注册表
    registry = AdapterRegistry()
    registry.load_all()

    # 中间件
    request_mw = _build_request_middlewares(config, http_client)
    response_mw = _build_response_middlewares(config)

    # 管道
    pipelines = _build_pipelines(config)

    return storage, http_client, registry, request_mw, response_mw, pipelines


def _build_request_middlewares(config: AppConfig, http_client=None):
    middlewares = []
    mw_config = config.middleware.get("request", [])

    for item in mw_config:
        for name, params in item.items():
            if name == "user_agent":
                from novel.middlewares.user_agent import UserAgentMiddleware

                middlewares.append(UserAgentMiddleware(**params))
            elif name == "rate_limit":
                from novel.middlewares.rate_limit import RateLimitMiddleware

                middlewares.append(RateLimitMiddleware(**params))
            # retry 现已作为响应中间件（重试信号由 Fetcher 统一执行退避），
            # 放在 request 下将被忽略——请移至 response: 下。

    return middlewares


def _get_max_retries(config: AppConfig) -> int:
    """从配置中读取重试次数（默认 3），request / response 两处皆可配置。"""
    for section in ("request", "response"):
        for item in config.middleware.get(section, []):
            for name, params in item.items():
                if name == "retry":
                    return int(params.get("max_retries", 3))
    return 3


def _build_response_middlewares(config: AppConfig):
    middlewares = []
    mw_config = config.middleware.get("response", [])

    for item in mw_config:
        for name, params in item.items():
            if name == "encoding":
                from novel.middlewares.encoding import EncodingMiddleware

                middlewares.append(EncodingMiddleware(**params))
            elif name == "content_verify":
                from novel.middlewares.content_verify import ContentVerifyMiddleware

                middlewares.append(ContentVerifyMiddleware(**params))
            elif name == "retry":
                from novel.middlewares.retry import RetryMiddleware

                middlewares.append(RetryMiddleware(**params))

    return middlewares


def _build_pipelines(config: AppConfig):
    pipelines = []
    pipe_config = config.pipeline

    if pipe_config.get("html_cleaner", {}).get("enabled", True):
        from novel.pipelines.html_cleaner import HTMLCleanerPipeline

        pipelines.append(HTMLCleanerPipeline())

    if pipe_config.get("text_normalizer", {}).get("enabled", True):
        from novel.pipelines.text_normalizer import TextNormalizerPipeline

        params = {k: v for k, v in pipe_config["text_normalizer"].items() if k != "enabled"}
        pipelines.append(TextNormalizerPipeline(**params))

    if pipe_config.get("deduplication", {}).get("enabled", True):
        from novel.pipelines.deduplication import DeduplicationPipeline

        pipelines.append(DeduplicationPipeline())

    if pipe_config.get("quality_validator", {}).get("enabled", True):
        from novel.pipelines.quality_validator import QualityValidatorPipeline

        params = {k: v for k, v in pipe_config["quality_validator"].items() if k != "enabled"}
        pipelines.append(QualityValidatorPipeline(**params))

    return pipelines


def run_async(coro):
    """运行异步函数，确保资源正确清理。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        # 等待待处理任务完成，避免 aiosqlite 线程报错
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()


# ---- 登录辅助 ----

# 账号密码读取优先级：CLI 显式参数 > 环境变量（避免在配置文件中存明文密码）
WENKU8_USER_ENV = "WENKU8_USERNAME"
WENKU8_PASS_ENV = "WENKU8_PASSWORD"


def resolve_credentials(cli_username: str | None, cli_password: str | None):
    """解析站点登录凭据。

    优先级：CLI 参数 > 环境变量 WENKU8_USERNAME / WENKU8_PASSWORD。
    不把密码写进配置文件，降低泄露风险。
    """
    username = cli_username or os.environ.get(WENKU8_USER_ENV)
    password = cli_password or os.environ.get(WENKU8_PASS_ENV)
    return username, password


async def maybe_login(adapter, username: str | None, password: str | None) -> bool:
    """若提供了凭据且适配器支持登录，则执行登录。

    Returns:
        是否成功触发登录（未提供凭据返回 False，不视为错误）。
    """
    if not username or not password:
        return False
    if not hasattr(adapter, "login"):
        logger.warning(f"适配器 {getattr(adapter, 'site_name', '?')} 不支持登录，跳过")
        return False
    try:
        ok = await adapter.login(username, password)
        if not ok:
            logger.warning("登录未成功（账号/密码错误或站点限制）")
        return bool(ok)
    except Exception as e:  # noqa: BLE001
        logger.error(f"登录过程异常: {e}")
        return False


# ---- CLI 命令组 ----


@click.group()
@click.option("--config", "config_path", default=None, help="配置文件路径")
@click.option("--db", "db_path", default=None, help="数据库路径")
@click.option("--log-level", default=None, help="日志级别 (debug/info/warning/error)")
@click.option("--json", "json_output", is_flag=True, help="JSON 格式输出")
@click.pass_context
def cli(ctx, config_path, db_path, log_level, json_output):
    """通用轻小说爬虫框架"""
    config = AppConfig.load(config_path)
    if db_path:
        config.storage.sqlite.path = db_path
    if log_level:
        config.logging.level = log_level

    setup_logging(config)
    ctx.ensure_object(dict)
    ctx.obj["config"] = config
    ctx.obj["json"] = json_output


# ---- 站点管理 ----


@cli.group()
def sites():
    """站点管理"""


@sites.command("list")
@click.pass_context
def sites_list(ctx):
    """列出可用站点"""
    config = ctx.obj["config"]
    _, _, registry, _, _, _ = create_engine_from_config(config)

    sites_list_data = registry.list_sites()
    if ctx.obj["json"]:
        click.echo(json.dumps(sites_list_data, ensure_ascii=False))
    else:
        click.echo("可用站点:")
        for site in sites_list_data:
            adapter = registry.get(site)
            click.echo(f"  {site:15s}  {adapter.base_url}  (限速: {adapter.get_rate_limit()}s)")


@sites.command("info")
@click.argument("site_name")
@click.pass_context
def sites_info(ctx, site_name):
    """查看站点信息"""
    config = ctx.obj["config"]
    _, _, registry, _, _, _ = create_engine_from_config(config)

    try:
        adapter = registry.get(site_name)
    except Exception as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)

    info = {
        "site_name": adapter.site_name,
        "base_url": adapter.base_url,
        "encoding": adapter.encoding,
        "rate_limit": adapter.get_rate_limit(),
    }

    if ctx.obj["json"]:
        click.echo(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        click.echo(f"站点:   {info['site_name']}")
        click.echo(f"URL:    {info['base_url']}")
        click.echo(f"编码:   {info['encoding']}")
        click.echo(f"限速:   {info['rate_limit']}s")


@sites.command("login")
@click.argument("site_name")
@click.option("--username", default=None, help="登录账号（也可用环境变量 WENKU8_USERNAME）")
@click.option("--password", default=None, help="登录密码（也可用环境变量 WENKU8_PASSWORD）")
@click.pass_context
def sites_login(ctx, site_name, username, password):
    """登录指定站点（验证账号密码 / 获取会话 Cookie）"""
    config = ctx.obj["config"]
    _, http_client, registry, _, _, _ = create_engine_from_config(config)

    async def _login():
        try:
            adapter = registry.get(site_name)
            adapter._http_client = http_client

            user, pwd = resolve_credentials(username, password)
            if not user or not pwd:
                click.echo(
                    "错误: 未提供账号/密码。请用 --username/--password "
                    "或环境变量 WENKU8_USERNAME / WENKU8_PASSWORD。",
                    err=True,
                )
                sys.exit(1)

            ok = await maybe_login(adapter, user, pwd)
            if ok:
                click.echo(f"[{site_name}] 登录成功")
            else:
                click.echo(f"[{site_name}] 登录失败（账号/密码错误或站点限制）", err=True)
                sys.exit(1)

            await adapter.close()
            await http_client.close()
        except Exception as e:
            click.echo(f"错误: {e}", err=True)
            sys.exit(1)

    run_async(_login())


# ---- 搜索 ----


@cli.command()
@click.argument("site_name")
@click.argument("keyword")
@click.option("--page", default=1, help="页码")
@click.pass_context
def search(ctx, site_name, keyword, page):
    """搜索小说"""
    config = ctx.obj["config"]
    _, http_client, registry, _, _, _ = create_engine_from_config(config)

    async def _search():
        try:
            adapter = registry.get(site_name)
            adapter._http_client = http_client
            results, has_next = await adapter.search(keyword, page)

            if ctx.obj["json"]:
                data = [
                    {
                        "site_id": r.site_id,
                        "title": r.title,
                        "author": r.author,
                    }
                    for r in results
                ]
                click.echo(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                if not results:
                    click.echo("未找到结果")
                    return
                click.echo(f"搜索 '{keyword}' 结果 (第 {page} 页):")
                click.echo("-" * 60)
                for r in results:
                    click.echo(f"  [{r.site_id}] {r.title}")
                    if r.author:
                        click.echo(f"        作者: {r.author}")
                if has_next:
                    click.echo(f"\n使用 --page {page+1} 查看下一页")

            await adapter.close()
            await http_client.close()
        except Exception as e:
            click.echo(f"错误: {e}", err=True)
            sys.exit(1)

    run_async(_search())


# ---- 爬取 ----


@cli.command()
@click.argument("site_name")
@click.argument("novel_id", required=False)
@click.option("--chapters", default=None, help="章节范围 (如 1-10)")
@click.option("--resume", "resume_task_id", default=None, help="断点续爬的任务 ID")
@click.option("--delay", "delay", default=None, type=float, help="请求间隔（秒），覆盖默认限速（仅作用于插图下载限速）")
@click.option("--image-concurrency", "image_concurrency", default=4, type=int, help="章节插图并发下载数")
@click.option("--username", default=None, help="站点登录账号（wenku8 等需登录站点；也可用环境变量 WENKU8_USERNAME）")
@click.option("--password", default=None, help="站点登录密码（也可用环境变量 WENKU8_PASSWORD，避免明文出现在命令历史）")
@click.pass_context
def crawl(ctx, site_name, novel_id, chapters, resume_task_id, delay, image_concurrency, username, password):
    """爬取指定小说。可用 --resume <task_id> 断点续爬。需登录站点可加 --username/--password（或环境变量）"""
    config = ctx.obj["config"]

    if not novel_id and not resume_task_id:
        click.echo("错误: 必须提供 novel_id，或使用 --resume <task_id> 续爬", err=True)
        sys.exit(1)

    chapter_range = None
    if chapters:
        parts = chapters.split("-")
        if len(parts) == 2:
            chapter_range = (int(parts[0]), int(parts[1]))
        else:
            chapter_range = (1, int(parts[0]))

    async def _crawl():
        storage, http_client, registry, req_mw, resp_mw, pipelines = create_engine_from_config(config)

        # 续爬时从任务状态解析真实的 site / novel_id（用独立变量，避免遮蔽外层参数）
        resolved_site = site_name
        resolved_novel = novel_id

        try:
            adapter = registry.get(resolved_site)
            adapter._http_client = http_client

            # 登录（如站点支持且提供了凭据）：登录态贯穿本次爬取的全部请求
            await maybe_login(adapter, *resolve_credentials(username, password))

            await storage.init()

            # 续爬：从任务状态解析 novel_id / site
            if resume_task_id:
                state = await storage.load_state(resume_task_id)
                if not state:
                    click.echo(f"错误: 未找到续爬任务 {resume_task_id}", err=True)
                    sys.exit(1)
                resolved_novel = state["novel_id"]
                if state.get("site"):
                    resolved_site = state["site"]
                    # 若与命令行 site 不同，切换适配器
                    adapter = registry.get(resolved_site)
                    adapter._http_client = http_client

            from novel.engine.crawler import CrawlerEngine

            engine = CrawlerEngine(
                adapter=adapter,
                storage=storage,
                http_client=http_client,
                request_middlewares=req_mw,
                response_middlewares=resp_mw,
                pipelines=pipelines,
                max_concurrency=config.engine.max_concurrency,
                rate_limit=delay if delay is not None else max(
                    config.engine.default_rate_limit, adapter.get_rate_limit()
                ),
                chapter_range=chapter_range,
                max_retries=_get_max_retries(config),
                resume_task_id=resume_task_id,
                image_concurrency=image_concurrency,
            )

            result = await engine.crawl_novel(resolved_novel)

            click.echo(f"\n爬取完成:")
            click.echo(f"  小说: {result.novel.title}")
            click.echo(f"  成功: {len(result.chapters)} 章")
            click.echo(f"  失败: {len(result.failed_chapters)} 章")
            click.echo(f"  跳过: {len(result.skipped_chapters)} 章")

            await engine.aclose()
            await adapter.close()
            await http_client.close()
            await storage.close()
        except Exception as e:
            logger.error(f"爬取失败: {e}")
            click.echo(f"错误: {e}", err=True)
            sys.exit(1)

    run_async(_crawl())


# ---- 全站爬取 ----


@cli.command("crawl-all")
@click.argument("site_name")
@click.option("--max-novels", default=None, type=int, help="最多爬取多少本小说")
@click.option("--start-id", default=None, type=int, help="起始小说 ID（wenku8 ID 遍历用；不填则自动从已存精确数量+1 续爬）")
@click.option("--auto-export", is_flag=True, help="爬取完成后自动导出 EPUB")
@click.option("--delay", "delay", default=None, type=float, help="请求间隔（秒），覆盖默认限速（仅作用于插图下载限速）")
@click.option("--image-concurrency", "image_concurrency", default=4, type=int, help="章节插图并发下载数")
@click.option("--workers", default=1, type=int, help="多线程 worker 数（>=2 启用多线程池；默认 1 单线程）。每个 worker 独立引擎实例=独立限速预算，≈N× 吞吐")
@click.option("--shard", "shard_mode", is_flag=True, help="分块领取（需 --workers>=2）：按 BLOCK_SIZE=500 分块，块内所有 worker 按序号最低空闲书号细粒度领取，整块领完切下一块；整空白块判定末尾并记录总书数")
@click.option("--username", default=None, help="站点登录账号（wenku8 等需登录站点；也可用环境变量 WENKU8_USERNAME）")
@click.option("--password", default=None, help="站点登录密码（也可用环境变量 WENKU8_PASSWORD）")
@click.pass_context
def crawl_all(ctx, site_name, max_novels, start_id, auto_export, delay, image_concurrency, workers, shard_mode, username, password):
    """全站爬取：遍历站点所有小说并逐一爬取

    wenku8 采用 ID 递增遍历（不设上限）：从起始 ID 到无穷，遇到连续 100 个
    无效编号即判定到站点末尾。首次爬取从 1 开始；之后自动从上次保存的精确
    数量 +1 续爬（--start-id 可手动覆盖起点）。--max-novels 限制数量。

    多线程：--workers 2 启用多线程池（每 worker 独立引擎+独立限速预算，≈2× 吞吐）；
    再加 --shard 进入分块领取模式：外层 500 一本分块，块内按最低空闲书号细粒度并行领取、
    无尾段空等。--workers 1 退化为单线程。
    """
    config = ctx.obj["config"]

    filter_params = {}
    if start_id is not None:
        filter_params["start_id"] = start_id

    async def _crawl_all():
        # 轻量能力探测（不创建完整引擎链路）
        _, _, registry, _, _, _ = create_engine_from_config(config)
        try:
            probe_adapter = registry.get(site_name)
        except Exception as e:
            click.echo(f"错误: {e}", err=True)
            sys.exit(1)
        if not probe_adapter.supports_site_wide():
            click.echo(
                f"错误: 站点 '{site_name}' 的适配器未实现全站遍历（iter_all_novels），"
                f"无法使用 crawl-all。请改用 crawl 单本爬取。",
                err=True,
            )
            sys.exit(1)

        # 多线程池模式（workers >= 2）：每 worker 独立引擎 = 独立限速预算
        if workers >= 2:
            from novel.engine.worker_pool import CrawlWorkerPool

            user, pwd = resolve_credentials(username, password)
            pool = CrawlWorkerPool(
                config=config,
                site=site_name,
                db_path=config.storage.sqlite.path,
                workers=workers,
                username=user,
                password=pwd,
                rate_limit=delay,
                image_concurrency=image_concurrency,
                max_concurrency=config.engine.max_concurrency,
                mode="shard" if shard_mode else "discover",
            )
            try:
                stats = await pool.arun(max_novels=max_novels, start_id=start_id)
            except Exception as e:
                logger.error(f"多线程全站爬取失败: {e}")
                click.echo(f"错误: {e}", err=True)
                sys.exit(1)

            click.echo(f"\n多线程全站爬取完成 (模式={pool.mode}, workers={workers}):")
            click.echo(f"  本批爬取: {stats['novels']} 本")
            click.echo(f"  成功: {stats['ok']} 本")
            click.echo(f"  失败: {stats['fail']} 本")
            click.echo(f"  章节成功: {stats['chapters']}")

            if auto_export:
                await _export_site(config, site_name)
            return

        # ---- 单线程模式（workers == 1，原有路径）----
        storage, http_client, registry, req_mw, resp_mw, pipelines = create_engine_from_config(config)

        try:
            adapter = registry.get(site_name)
            adapter._http_client = http_client

            # 登录（如站点支持且提供了凭据）
            await maybe_login(adapter, *resolve_credentials(username, password))

            await storage.init()

            from novel.engine.crawler import CrawlerEngine

            engine = CrawlerEngine(
                adapter=adapter,
                storage=storage,
                http_client=http_client,
                request_middlewares=req_mw,
                response_middlewares=resp_mw,
                pipelines=pipelines,
                max_concurrency=config.engine.max_concurrency,
                rate_limit=delay if delay is not None else max(
                    config.engine.default_rate_limit, adapter.get_rate_limit()
                ),
                max_retries=_get_max_retries(config),
                image_concurrency=image_concurrency,
            )

            results = await engine.crawl_site(
                max_novels=max_novels,
                novel_filter=filter_params if filter_params else None,
            )

            click.echo(f"\n全站爬取完成:")
            click.echo(f"  总计: {len(results)} 本")
            click.echo(f"  成功: {sum(1 for r in results if r.chapters)} 本")
            click.echo(f"  失败: {sum(1 for r in results if not r.chapters)} 本")

            total_chapters = sum(len(r.chapters) for r in results)
            total_skipped = sum(len(r.skipped_chapters) for r in results)
            total_failed = sum(len(r.failed_chapters) for r in results)
            click.echo(f"  章节成功: {total_chapters}, 跳过: {total_skipped}, 失败: {total_failed}")

            # 自动导出
            if auto_export and results:
                click.echo("\n开始自动导出 EPUB...")
                from novel.exporters.epub import EPUBExporter

                exporter = EPUBExporter(
                    storage=storage,
                    output_dir=config.export.output_dir,
                )

                for r in results:
                    if not r.chapters:
                        continue
                    try:
                        novels_list = await storage.get_novel_list(site_name)
                        for n in novels_list:
                            if n["site_novel_id"] == r.novel.site_id:
                                path = await exporter.export(n["id"])
                                click.echo(f"  已导出: {r.novel.title} -> {path}")
                                break
                    except Exception as e:
                        logger.warning(f"导出失败 [{r.novel.title}]: {e}")

            await engine.aclose()
            await adapter.close()
            await http_client.close()
            await storage.close()
        except Exception as e:
            logger.error(f"全站爬取失败: {e}")
            click.echo(f"错误: {e}", err=True)
            sys.exit(1)

    run_async(_crawl_all())


async def _export_site(config: "AppConfig", site_name: str) -> None:
    """导出指定站点全部已存小说为 EPUB（供多线程池模式复用）。"""
    from novel.exporters.epub import EPUBExporter

    storage, _, _, _, _, _ = create_engine_from_config(config)
    await storage.init()
    try:
        novels = await storage.get_novel_list(site_name)
        if not novels:
            return
        click.echo("\n开始自动导出 EPUB...")
        exporter = EPUBExporter(
            storage=storage,
            output_dir=config.export.output_dir,
            font_family=config.export.epub_font_family,
            base_font_size=config.export.epub_base_font_size,
            line_height=config.export.epub_line_height,
            paragraph_indent=config.export.epub_paragraph_indent,
            split_by_volume=config.export.epub_split_by_volume,
        )
        for n in novels:
            try:
                path = await exporter.export(n["id"])
                click.echo(f"  已导出: {n['title']} -> {path}")
            except Exception as e:
                logger.warning(f"导出失败 [{n['title']}]: {e}")
    finally:
        await storage.close()


# ---- 增量更新 ----


@cli.command()
@click.option("--site", "site_name", default=None, help="仅更新指定站点")
@click.option("--username", default=None, help="站点登录账号（wenku8 等需登录站点；也可用环境变量 WENKU8_USERNAME）")
@click.option("--password", default=None, help="站点登录密码（也可用环境变量 WENKU8_PASSWORD）")
@click.pass_context
def update(ctx, site_name, username, password):
    """增量更新已存小说"""
    config = ctx.obj["config"]

    async def _update():
        storage, http_client, registry, req_mw, resp_mw, pipelines = create_engine_from_config(config)

        await storage.init()
        novels = await storage.get_novel_list(site_name)

        if not novels:
            click.echo("没有已存的小说")
            return

        click.echo(f"检查 {len(novels)} 本小说的更新...")

        last_login_site = None
        for novel_row in novels:
            site = novel_row["site"]
            site_novel_id = novel_row["site_novel_id"]
            title = novel_row["title"]
            stored_updated = novel_row.get("last_updated")

            try:
                adapter = registry.get(site)
                adapter._http_client = http_client

                # 每个站点仅在首次遇到时登录一次
                if last_login_site != site:
                    await maybe_login(adapter, *resolve_credentials(username, password))
                    last_login_site = site

                latest = await adapter.get_novel_info(site_novel_id)

                if latest.last_updated and latest.last_updated != stored_updated:
                    click.echo(f"  [更新] {title}: {stored_updated} → {latest.last_updated}")

                    from novel.engine.crawler import CrawlerEngine

                    engine = CrawlerEngine(
                        adapter=adapter,
                        storage=storage,
                        http_client=http_client,
                        request_middlewares=req_mw,
                        response_middlewares=resp_mw,
                        pipelines=pipelines,
                        max_concurrency=config.engine.max_concurrency,
                        rate_limit=max(
                            config.engine.default_rate_limit, adapter.get_rate_limit()
                        ),
                        max_retries=_get_max_retries(config),
                    )
                    await engine.crawl_novel(site_novel_id)
                else:
                    click.echo(f"  [最新] {title}")

                await adapter.close()
            except Exception as e:
                click.echo(f"  [错误] {title}: {e}")

        await http_client.close()
        await storage.close()

    run_async(_update())


# ---- 导出 ----


@cli.command()
@click.argument("novel_id")
@click.option("--output", "output_path", default=None, help="输出文件路径")
@click.pass_context
def export(ctx, novel_id, output_path):
    """导出 EPUB"""
    config = ctx.obj["config"]

    async def _export():
        storage, _, _, _, _, _ = create_engine_from_config(config)
        await storage.init()

        from novel.exporters.epub import EPUBExporter

        exporter = EPUBExporter(
            storage=storage,
            output_dir=config.export.output_dir,
            font_family=config.export.epub_font_family,
            base_font_size=config.export.epub_base_font_size,
            line_height=config.export.epub_line_height,
            paragraph_indent=config.export.epub_paragraph_indent,
            split_by_volume=config.export.epub_split_by_volume,
        )

        try:
            path = await exporter.export(novel_id, output_path)
            click.echo(f"EPUB 已导出: {path}")
        except Exception as e:
            click.echo(f"导出失败: {e}", err=True)
            sys.exit(1)
        finally:
            await storage.close()

    run_async(_export())


# ---- 数据管理 ----


@cli.command("list")
@click.pass_context
def list_novels(ctx):
    """列出已爬取的小说"""
    config = ctx.obj["config"]

    async def _list():
        storage, _, _, _, _, _ = create_engine_from_config(config)
        await storage.init()
        novels = await storage.get_novel_list()

        if ctx.obj["json"]:
            click.echo(json.dumps(novels, ensure_ascii=False, indent=2, default=str))
        else:
            if not novels:
                click.echo("暂无已爬取的小说")
                return
            click.echo(f"共 {len(novels)} 本小说:")
            click.echo("-" * 80)
            for n in novels:
                click.echo(
                    f"  [{n['id'][:8]}] {n['title']} - {n.get('author', '?')} "
                    f"({n['site']})"
                )

        await storage.close()

    run_async(_list())


@cli.command()
@click.argument("novel_id")
@click.pass_context
def info(ctx, novel_id):
    """查看小说详情"""
    config = ctx.obj["config"]

    async def _info():
        storage, _, _, _, _, _ = create_engine_from_config(config)
        await storage.init()

        novel = await storage.get_novel_by_internal_id(novel_id)
        if not novel:
            # 尝试按 site_novel_id 查找
            novels = await storage.get_novel_list()
            for n in novels:
                if n.get("site_novel_id") == novel_id:
                    novel = await storage.get_novel_by_internal_id(n["id"])
                    novel_id = n["id"]
                    break

        if not novel:
            click.echo(f"未找到小说: {novel_id}")
            sys.exit(1)

        chapters = await storage.get_chapters(novel_id)

        click.echo(f"标题:   {novel.title}")
        click.echo(f"作者:   {novel.author}")
        click.echo(f"站点:   {novel.extra.get('site', '?')}")
        click.echo(f"状态:   {novel.status}")
        click.echo(f"字数:   {novel.word_count}")
        click.echo(f"章节数: {len(chapters)}")
        if novel.description:
            click.echo(f"简介:   {novel.description[:200]}...")

        await storage.close()

    run_async(_info())


@cli.command()
@click.pass_context
def stats(ctx):
    """统计信息"""
    config = ctx.obj["config"]

    async def _stats():
        storage, _, _, _, _, _ = create_engine_from_config(config)
        await storage.init()

        data = await storage.get_stats()

        if ctx.obj["json"]:
            click.echo(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            click.echo("统计信息:")
            click.echo(f"  小说数:   {data['novels']}")
            click.echo(f"  章节数:   {data['chapters']}")
            click.echo(f"  总字数:   {data['total_words']}")
            click.echo(f"  站点分布: {data['sites']}")

        await storage.close()

    run_async(_stats())


@cli.command()
@click.argument("novel_id")
@click.confirmation_option(prompt="确定删除该小说及所有关联数据？")
@click.pass_context
def delete(ctx, novel_id):
    """删除小说及关联数据"""
    config = ctx.obj["config"]

    async def _delete():
        storage, _, _, _, _, _ = create_engine_from_config(config)
        await storage.init()
        await storage.delete_novel(novel_id)
        click.echo(f"已删除: {novel_id}")
        await storage.close()

    run_async(_delete())


# ---- 任务管理 ----


@cli.group()
def tasks():
    """任务管理"""


@tasks.command("list")
@click.pass_context
def tasks_list(ctx):
    """列出任务"""
    config = ctx.obj["config"]

    async def _list():
        storage, _, _, _, _, _ = create_engine_from_config(config)
        await storage.init()
        task_list = await storage.list_tasks()

        if not task_list:
            click.echo("暂无任务")
            return

        click.echo(f"共 {len(task_list)} 个任务:")
        click.echo("-" * 80)
        for t in task_list:
            click.echo(
                f"  [{t['task_id'][:8]}] {t['site']}/{t['novel_id']} "
                f"- {t['status']} ({t.get('crawled_chapters', 0)}/"
                f"{t.get('total_chapters', 0)})"
            )

        await storage.close()

    run_async(_list())


@tasks.command("status")
@click.argument("task_id")
@click.pass_context
def tasks_status(ctx, task_id):
    """查看任务状态"""
    config = ctx.obj["config"]

    async def _status():
        storage, _, _, _, _, _ = create_engine_from_config(config)
        await storage.init()
        state = await storage.load_state(task_id)

        if not state:
            click.echo(f"未找到任务: {task_id}")
            sys.exit(1)

        click.echo(f"任务 ID:    {state['task_id']}")
        click.echo(f"站点:       {state['site']}")
        click.echo(f"小说 ID:    {state['novel_id']}")
        click.echo(f"状态:       {state['status']}")
        click.echo(f"进度:       {state.get('crawled_chapters', 0)}/{state.get('total_chapters', 0)}")
        if state.get("error_message"):
            click.echo(f"错误:       {state['error_message']}")

        await storage.close()

    run_async(_status())


@tasks.command("pause")
@click.argument("task_id")
@click.pass_context
def tasks_pause(ctx, task_id):
    """暂停任务（运行中的爬取会在下一章节边界停止）"""
    config = ctx.obj["config"]

    async def _pause():
        storage, _, _, _, _, _ = create_engine_from_config(config)
        await storage.init()
        from novel.engine.state import TaskManager

        manager = TaskManager(storage)
        try:
            await manager.pause_task(task_id)
            click.echo(f"已暂停任务: {task_id}")
        except Exception as e:
            click.echo(f"暂停失败: {e}", err=True)
            sys.exit(1)
        finally:
            await storage.close()

    run_async(_pause())


@tasks.command("resume")
@click.argument("task_id")
@click.pass_context
def tasks_resume(ctx, task_id):
    """恢复任务（状态置为 running，随后用 `crawl --resume <task_id>` 续爬）"""
    config = ctx.obj["config"]

    async def _resume():
        storage, _, _, _, _, _ = create_engine_from_config(config)
        await storage.init()
        from novel.engine.state import TaskManager

        manager = TaskManager(storage)
        try:
            state = await manager.resume_task(task_id)
            click.echo(f"已恢复任务: {task_id} (status={state['status']})")
            click.echo(f"续爬命令: novel crawl {state['site']} {state['novel_id']} --resume {task_id}")
        except Exception as e:
            click.echo(f"恢复失败: {e}", err=True)
            sys.exit(1)
        finally:
            await storage.close()

    run_async(_resume())


@tasks.command("delete")
@click.argument("task_id")
@click.confirmation_option(prompt="确定删除该任务记录？")
@click.pass_context
def tasks_delete(ctx, task_id):
    """删除任务记录"""
    config = ctx.obj["config"]

    async def _delete():
        storage, _, _, _, _, _ = create_engine_from_config(config)
        await storage.init()
        from novel.engine.state import TaskManager

        manager = TaskManager(storage)
        try:
            await manager.delete_task(task_id)
            click.echo(f"已删除任务: {task_id}")
        except Exception as e:
            click.echo(f"删除失败: {e}", err=True)
            sys.exit(1)
        finally:
            await storage.close()

    run_async(_delete())


# ---- 配置 ----


@cli.group()
def config():
    """配置管理"""


@config.command("init")
@click.option("--path", "output_path", default="novel.yaml", help="输出路径")
def config_init(output_path):
    """初始化配置文件"""
    src = Path(__file__).parent.parent.parent / "config" / "default.yaml"
    if not src.exists():
        click.echo(f"默认配置文件不存在: {src}")
        sys.exit(1)

    dst = Path(output_path)
    if dst.exists():
        click.echo(f"文件已存在: {dst}")
        if not click.confirm("覆盖？"):
            return

    dst.write_text(src.read_text("utf-8"), "utf-8")
    click.echo(f"配置文件已创建: {dst}")


@config.command("show")
@click.pass_context
def config_show(ctx):
    """查看当前配置"""
    config = ctx.obj["config"]

    click.echo(f"存储后端:   {config.storage.backend}")
    click.echo(f"数据库路径: {config.storage.sqlite.path}")
    click.echo(f"最大并发:   {config.engine.max_concurrency}")
    click.echo(f"默认限速:   {config.engine.default_rate_limit}s")
    click.echo(f"输出目录:   {config.export.output_dir}")
    click.echo(f"日志级别:   {config.logging.level}")


if __name__ == "__main__":
    cli()
