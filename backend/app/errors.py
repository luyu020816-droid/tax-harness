from dataclasses import dataclass, field


@dataclass
class AppError(Exception):
    code: str
    message: str
    status: int = 400
    retryable: bool = False
    details: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }
