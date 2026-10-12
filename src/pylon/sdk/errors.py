"""Bounded, redacted transport diagnostics for modules."""
import re
from pylon.config import redact

def safe_error(error):
    # Transport exceptions can contain response text, request URLs or headers.
    # Keep their class at the call site and only store a fixed diagnosis here.
    import httpx
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return '请求超时'
    if isinstance(error, (ConnectionError, httpx.NetworkError)):
        return '连接失败'
    return re.sub(r'https?://\S+', '[url]', redact(str(error)))[:300]
