import logging
import os
import sys


def get_logger(name: str) -> logging.Logger:
    lg = logging.getLogger(name)
    if lg.handlers:
        return lg
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    lg.addHandler(h)
    lg.setLevel(os.getenv("LOG_LEVEL", "INFO"))
    lg.propagate = False
    return lg
