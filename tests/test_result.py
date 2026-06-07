"""Phase 1 — ExecResult / audit tests. Pure Python."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from temenos import AuditLog, ExecResult, PolicyDecision
from temenos.result import AuditEntry


# -- ExecResult -----------------------------------------------------------------------

def test_ok_true_on_zero_exit():
    assert ExecResult("out", "", 0).ok is True

def test_ok_false_on_nonzero():
    assert ExecResult("", "boom", 1).ok is False

def test_raise_for_status_returns_self_on_success():
    r = ExecResult("out", "", 0)
    assert r.raise_for_status() is r

def test_raise_for_status_raises_with_stderr():
    with pytest.raises(RuntimeError, match="boom"):
        ExecResult("", "boom", 3).raise_for_status()

def test_exec_result_to_dict():
    d = ExecResult("o", "e", 2, truncated=True, duration_ms=12).to_dict()
    assert d == {"stdout": "o", "stderr": "e", "exit_code": 2,
                 "truncated": True, "duration_ms": 12}


# -- audit ----------------------------------------------------------------------------

def test_audit_log_records_and_returns_entry():
    log = AuditLog()
    e = log.record("exec", PolicyDecision.ALLOW, {"cmd": ["echo", "hi"]}, box="b1")
    assert isinstance(e, AuditEntry)
    assert len(log) == 1
    assert list(log)[0] is e
    assert e.box == "b1"
    assert e.kind == "exec"

def test_audit_entry_to_dict_shape():
    ts = datetime(2026, 6, 7, tzinfo=timezone.utc)
    e = AuditEntry("network", PolicyDecision.DENY, {"host": "evil.com"}, box="b", timestamp=ts)
    d = e.to_dict()
    assert d["kind"] == "network"
    assert d["decision"] == "deny"
    assert d["details"] == {"host": "evil.com"}
    assert d["box"] == "b"
    assert d["timestamp"] == ts.isoformat()

def test_audit_log_to_dicts():
    log = AuditLog()
    log.record("exec", PolicyDecision.ALLOW)
    log.record("write", PolicyDecision.ALLOW, {"path": "/work/a"})
    dicts = log.to_dicts()
    assert len(dicts) == 2
    assert dicts[1]["details"] == {"path": "/work/a"}

def test_policy_decision_values():
    assert PolicyDecision.ALLOW.value == "allow"
    assert PolicyDecision.DENY.value == "deny"
    assert PolicyDecision.MODIFY.value == "modify"
