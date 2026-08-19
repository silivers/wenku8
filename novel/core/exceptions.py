"""自定义异常体系"""


class NovelCrawlerError(Exception):
    """框架基础异常"""


class AdapterNotFoundError(NovelCrawlerError):
    """适配器未找到"""


class RequestBlockedError(NovelCrawlerError):
    """请求被中间件拦截"""


class ItemDroppedError(NovelCrawlerError):
    """数据项被管道丢弃"""


class ParseError(NovelCrawlerError):
    """解析失败"""


class StorageError(NovelCrawlerError):
    """存储操作失败"""


class InvalidTaskStateError(NovelCrawlerError):
    """任务状态非法"""


class EncodingError(NovelCrawlerError):
    """编码处理失败"""


class ExportError(NovelCrawlerError):
    """导出失败"""


class FetchError(NovelCrawlerError):
    """抓取在重试耗尽后仍失败（被中间件拦截或网络异常）。

    适配器在 fetch_text 中将其抛出，引擎据此把章节计入 failed 而非当正文入库。
    """
