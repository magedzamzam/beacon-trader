import logging
import os
import sys

_FMT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

# ANSI SGR by level. INFO stays uncolored so routine lines don't add noise;
# warnings/errors are pre-attentively obvious on a fast-scrolling terminal (#46).
_LEVEL_COLOR = {
    "DEBUG": "\033[90m",            # dim grey
    "INFO": "",
    "WARNING": "\033[33m",          # yellow
    "ERROR": "\033[31m",            # red
    "CRITICAL": "\033[1;37;41m",    # bold white on red
}
_RESET = "\033[0m"


class _ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        _msg = super().format(record)
        _c = _LEVEL_COLOR.get(record.levelname, "")
        return f"{_c}{_msg}{_RESET}" if _c else _msg


def get_logger(name: str) -> logging.Logger:
    lg = logging.getLogger(name)
    if lg.handlers:
        return lg
    h = logging.StreamHandler(sys.stdout)
    # Color only on a real TTY (opt-out via LOG_COLOR=0) so piped/redirected
    # output and file/JSON sinks stay byte-identical and grep-friendly.
    _use_color = sys.stdout.isatty() and os.getenv("LOG_COLOR", "1") != "0"
    h.setFormatter(_ColorFormatter(_FMT) if _use_color else logging.Formatter(_FMT))
    lg.addHandler(h)
    lg.setLevel(os.getenv("LOG_LEVEL", "INFO"))
    lg.propagate = False
    return lg
