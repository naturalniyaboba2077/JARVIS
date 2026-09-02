"""Журнал Jarvis.

Один общий логгер на всё ядро: пишет в logs/jarvis_ГГГГ-ММ-ДД.log, по файлу
на день. Именно сюда стоит смотреть, когда Джарвис запущен через ярлык и
консоли нет.

Вынесено отдельным модулем, потому что логгер нужен почти каждой части ядра,
а тянуть ради него весь jarvis.py нельзя — получились бы круговые импорты.
"""

import datetime
import logging

from jarvis_config import JARVIS_DIR

__all__ = ["LOGS_DIR", "log_filename", "jarvis_logger", "log_interaction"]


LOGS_DIR = JARVIS_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
log_filename = LOGS_DIR / f"jarvis_{datetime.datetime.now().strftime('%Y-%m-%d')}.log"
_log_fh = logging.FileHandler(str(log_filename), encoding="utf-8", mode="a")
_log_fh.setLevel(logging.DEBUG)
_log_fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s"))
jarvis_logger = logging.getLogger("jarvis")
jarvis_logger.setLevel(logging.DEBUG)
if not jarvis_logger.handlers:
    jarvis_logger.addHandler(_log_fh)
jarvis_logger.propagate = False

def log_interaction(role: str, text: str):
    """Log user commands and Jarvis replies to daily log file."""
    jarvis_logger.info(f"[{role.upper()}] {text}")
