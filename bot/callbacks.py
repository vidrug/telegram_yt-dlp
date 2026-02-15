"""CallbackData classes for inline keyboard buttons."""

from aiogram.filters.callback_data import CallbackData


class FormatCallback(CallbackData, prefix="f"):
    session: str  # 8 chars
    fmt: str      # format_id


class PageCallback(CallbackData, prefix="p"):
    session: str
    page: int


class CancelCallback(CallbackData, prefix="c"):
    session: str


class SponsorBlockCallback(CallbackData, prefix="sb"):
    session: str
    fmt: str
    remove: int  # 1 = yes, 0 = no


class RawFormatsCallback(CallbackData, prefix="rf"):
    session: str


class RetryCallback(CallbackData, prefix="rt"):
    session: str
    fmt: str
    remove: int  # sponsorblock: 1 = yes, 0 = no
