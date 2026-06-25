from plugins.web.trafilatura.provider import TrafilaturaExtractProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(TrafilaturaExtractProvider())
