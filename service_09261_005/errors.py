"""领域错误类型。

NotFoundError 继承 KeyError（对应 404），其余继承 ValueError（对应 400），
ConflictError 单独标识版本/幂等冲突（对应 409）。
"""


class DomainError(ValueError):
    """所有可向调用方展示的业务规则错误。"""


class NotFoundError(KeyError):
    """观察记录不存在。"""


class ConflictError(DomainError):
    """状态已被他人推进或幂等键对应了不同请求。"""


class ValidationError(DomainError):
    """入参不满足业务约束。"""
