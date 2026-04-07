import logging
import logging.handlers
from pathlib import Path

LOGS_DIR = Path(__file__).parent.parent / "logs"


def setup_logging(level: int = logging.INFO) -> None:
    """Set up logging with both console and rotating file handlers."""
    LOGS_DIR.mkdir(exist_ok=True)

    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    # Console
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    root.addHandler(ch)

    # Rotating file (10 MB × 5 files)
    fh = logging.handlers.RotatingFileHandler(
        LOGS_DIR / "news_monitor.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(formatter)
    root.addHandler(fh)

    # Suppress noisy third-party loggers
    for lib in ("urllib3", "requests", "feedparser", "httpx", "httpcore"):
        logging.getLogger(lib).setLevel(logging.WARNING)
