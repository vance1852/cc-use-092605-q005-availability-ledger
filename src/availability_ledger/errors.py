"""可用率权益账本向 API 和 CLI 暴露的稳定错误。"""


class LedgerError(RuntimeError):
    code = "ledger_error"
    status = 400


class NotFound(LedgerError):
    code = "not_found"
    status = 404


class Conflict(LedgerError):
    code = "conflict"
    status = 409


class Forbidden(LedgerError):
    code = "forbidden"
    status = 403


class InvalidState(LedgerError):
    code = "invalid_state"
    status = 409


class ValidationFailed(LedgerError):
    code = "validation_failed"
    status = 422
