import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from test_cli import parquet, write_contract

from datacontracts.orchestrate import ContractFailed, alert_text, check, failed_rules, send_alert


def test_check_passes_and_returns_report(tmp_path):
    src = parquet(tmp_path, [(1, "x"), (2, "y")])
    rep = check(write_contract(tmp_path), src)
    assert rep["ok"] and rep["rows"] == 2


def test_missing_file_is_retryable_not_a_contract_failure(tmp_path):
    with pytest.raises(FileNotFoundError):
        check(write_contract(tmp_path), str(tmp_path / "nope.parquet"))


def test_violation_raises_contract_failed_with_report(tmp_path):
    src = parquet(tmp_path, [(1, "x"), (-5, "y"), (-6, "z")])
    with pytest.raises(ContractFailed) as e:
        check(write_contract(tmp_path), src)
    assert e.value.report["rows"] == 3
    bad = failed_rules(e.value.report)
    assert [r["violations"] for r in bad] == [2]
    assert "2 violations" in str(e.value)


def test_exception_types_are_distinct():
    assert not issubclass(ContractFailed, OSError)
    assert not issubclass(FileNotFoundError, ContractFailed)


def _report(n_bad):
    return {
        "dataset": "d",
        "version": "1.0.0",
        "rows": 1000,
        "ok": False,
        "results": [
            {
                "rule": f"r{i}",
                "severity": "error",
                "violations": 10 + i,
                "rate": (10 + i) / 1000,
                "allowed_rate": 0.0,
                "passed": False,
            }
            for i in range(n_bad)
        ]
        + [
            {"rule": "w", "severity": "warn", "violations": 9, "rate": 0.009, "allowed_rate": 0.0, "passed": False},
            {"rule": "p", "severity": "error", "violations": 0, "rate": 0.0, "allowed_rate": 0.0, "passed": True},
        ],
    }


def test_failed_rules_excludes_warnings_and_passes():
    assert [r["rule"] for r in failed_rules(_report(2))] == ["r0", "r1"]


def test_alert_text_truncates_after_limit():
    text = alert_text("/x/file.parquet", _report(7), limit=5)
    assert "file.parquet" in text and "7 rule(s)" in text and "1,000 rows" in text
    assert text.count("\n- ") == 6 and text.endswith("and 2 more")
    assert "r0: 10 violations (1.0000%" in text


def test_alert_text_has_no_warning_lines():
    assert "w:" not in alert_text("f", _report(1))


def test_send_alert_writes_log(tmp_path):
    log = tmp_path / "sub" / "a.log"
    assert send_alert("hello", log_path=str(log)) == ["log"]
    send_alert("again", log_path=str(log))
    body = log.read_text()
    assert body.count("hello") == 1 and body.count("again") == 1


def test_send_alert_no_channels_is_noop(monkeypatch):
    monkeypatch.delenv("DATACONTRACTS_ALERT_LOG", raising=False)
    monkeypatch.delenv("DATACONTRACTS_WEBHOOK", raising=False)
    assert send_alert("x") == []


def test_send_alert_posts_json_to_webhook(tmp_path):
    got = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            got["ctype"] = self.headers["Content-Type"]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        used = send_alert("boom", webhook=f"http://127.0.0.1:{srv.server_port}/")
    finally:
        srv.shutdown()
    assert used == ["webhook"]
    assert got["body"] == {"text": "boom"} and got["ctype"] == "application/json"


def test_webhook_failure_is_reported_not_raised(tmp_path):
    used = send_alert("boom", log_path=str(tmp_path / "l"), webhook="http://127.0.0.1:1/")
    assert used[0] == "log" and used[1].startswith("webhook-failed:")


def test_alert_text_exactly_at_limit_has_no_more_line():
    text = alert_text("f", _report(5), limit=5)
    assert "more" not in text and text.count("\n- ") == 5
