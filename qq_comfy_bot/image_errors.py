"""Stable failure codes and bounded recovery hints for image receipts."""


class ImageFailure(RuntimeError):
    def __init__(self, code, stage, detail='', retryable=False):
        super().__init__(detail or code)
        self.code, self.stage, self.retryable = code, stage, retryable

    def receipt(self):
        return dict(error_code=self.code, failed_stage=self.stage,
                    recovery='resume_same_prompt' if self.retryable and self.stage in ('history','download') else 'retry_connection' if self.retryable else 'manual_review',
                    detail=str(self)[:800])
