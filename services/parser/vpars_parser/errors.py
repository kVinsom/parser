from .models import ErrorDetail


class ParserError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = 502,
        source_status: str | None = None,
        fields: list[str] | None = None,
        retry_after: int | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.detail = ErrorDetail(
            code=code,
            message=message,
            source_status=source_status,
            fields=fields or [],
            retry_after=retry_after,
        )
