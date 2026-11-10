import json
import logging

from alay.logging import JsonFormatter


def test_json_formatter_includes_extra_fields():
    rec = logging.LogRecord("alay", logging.INFO, __file__, 1, "check_finished", None, None)
    rec.adapter_id = "x"
    rec.job_id = "j"
    out = json.loads(JsonFormatter().format(rec))
    assert out["msg"] == "check_finished" and out["adapter_id"] == "x" and out["job_id"] == "j"
    assert out["level"] == "INFO" and "ts" in out
