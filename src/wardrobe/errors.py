from uuid import uuid4


class BusinessError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status

    def response(self) -> dict:
        return {
            "schema_version": "wardrobe/1.0",
            "request_id": str(uuid4()),
            "status": "failed",
            "error": {"code": self.code, "message": self.message},
        }
