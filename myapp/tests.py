import logging
import tempfile
from datetime import date
from pathlib import Path

from django.test import TestCase

from myproject.observability import DailyStructuredFileHandler


class DailyStructuredFileHandlerTests(TestCase):
    def test_daily_file_handler_writes_current_day_file_and_cleans_old_ones(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_dir = Path(tmp_dir)
            handler = DailyStructuredFileHandler(
                log_dir=log_dir,
                filename_prefix="application",
                retention_days=14,
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            handler._today = lambda: date(2026, 3, 22)

            (log_dir / "application-2026-03-01.log").write_text("old\n", encoding="utf-8")
            (log_dir / "application-2026-03-08.log").write_text("keep\n", encoding="utf-8")

            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname=__file__,
                lineno=1,
                msg="hello world",
                args=(),
                exc_info=None,
            )
            handler.emit(record)
            handler.close()

            current_log = log_dir / "application-2026-03-22.log"

            self.assertTrue(current_log.exists())
            self.assertIn("hello world", current_log.read_text(encoding="utf-8"))
            self.assertFalse((log_dir / "application-2026-03-01.log").exists())
            self.assertTrue((log_dir / "application-2026-03-08.log").exists())
