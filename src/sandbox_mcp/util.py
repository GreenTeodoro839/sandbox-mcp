"""Small shared helpers."""


def smart_decode(data: bytes) -> str:
    """Decode bytes as text, tolerating non-UTF-8 (e.g. GBK/GB18030 Chinese files).

    Tries strict UTF-8 first (the common case), then GB18030 (superset of
    GBK/GB2312), and finally UTF-8 with replacement so it never raises.
    """
    if not data:
        return ""
    for enc in ("utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")
