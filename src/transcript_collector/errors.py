"""Collection failures shared by application logic and infrastructure adapters."""


class SourceError(RuntimeError):
    pass


class CaptionsUnavailable(SourceError):
    pass


class SourceBlocked(SourceError):
    pass


class VideoNotReady(SourceError):
    pass


class TranscriptionError(RuntimeError):
    pass
