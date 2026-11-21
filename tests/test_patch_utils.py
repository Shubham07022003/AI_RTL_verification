"""
Tests for patch_utils.py - the pure, dependency-free debug-fix logic.

Runs without Groq / LangChain / Icarus Verilog.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from patch_utils import (
    PatchError,
    apply_patch_to_file,
    apply_patch_to_text,
    build_debug_email_body,
    build_debug_report,
    count_occurrences,
    detect_error_reason,
    extract_json,
    parse_patch,
    parse_root_cause,
    validate_patch,
)


# ------------------------------------------------------------------
# detect_error_reason
# ------------------------------------------------------------------

def test_detect_error_compile_failure():
    state = {"simulation_result": {"compile_status": "FAIL", "simulation_status": "NOT_RUN"}}
    trigger, reason = detect_error_reason(state)
    assert trigger is True
    assert "Compilation failed" in reason


def test_detect_error_simulation_failure():
    state = {"simulation_result": {"compile_status": "PASS", "simulation_status": "FAIL"}}
    trigger, reason = detect_error_reason(state)
    assert trigger is True
    assert "Simulation failed" in reason


def test_detect_error_log_errors():
    state = {
        "simulation_result": {"compile_status": "PASS", "simulation_status": "PASS"},
        "log_analysis": {"compile_error_count": 2, "simulation_error_count": 0},
    }
    trigger, reason = detect_error_reason(state)
    assert trigger is True
    assert "2 compile error" in reason


def test_detect_error_static_findings():
    state = {
        "simulation_result": {"compile_status": "PASS", "simulation_status": "PASS"},
        "log_analysis": {"compile_error_count": 0, "simulation_error_count": 0},
        "rtl_analysis": {"findings": ["No reset logic detected."]},
    }
    trigger, reason = detect_error_reason(state)
    assert trigger is True
    assert "No reset logic" in reason


def test_detect_error_none_found():
    state = {
        "simulation_result": {"compile_status": "PASS", "simulation_status": "PASS"},
        "log_analysis": {"compile_error_count": 0, "simulation_error_count": 0},
        "rtl_analysis": {"findings": []},
    }
    trigger, _ = detect_error_reason(state)
    assert trigger is False


# ------------------------------------------------------------------
# extract_json
# ------------------------------------------------------------------

def test_extract_json_plain():
    obj = extract_json('{"a": 1, "b": "x"}')
    assert json.loads(obj) == {"a": 1, "b": "x"}


def test_extract_json_fenced_and_prose():
    text = 'Here is the analysis:\n```json\n{"root_cause": "broken", "risk": "HIGH"}\n```\nDone.'
    obj = extract_json(text)
    assert json.loads(obj) == {"root_cause": "broken", "risk": "HIGH"}


def test_extract_json_with_nested_braces_and_strings():
    text = 'prefix {"a": {"b": ["c"]}, "s": "}{"} suffix'
    obj = extract_json(text)
    assert json.loads(obj) == {"a": {"b": ["c"]}, "s": "}{"}


def test_extract_json_none_on_garbage():
    assert extract_json("no json here") is None
    assert extract_json("") is None
    assert extract_json(None) is None


# ------------------------------------------------------------------
# parse_root_cause
# ------------------------------------------------------------------

def test_parse_root_cause_valid():
    text = '{"root_cause": "reset is wrong", "evidence": "line 12", "risk": "high"}'
    result = parse_root_cause(text)
    assert result["root_cause"] == "reset is wrong"
    assert result["risk"] == "HIGH"


def test_parse_root_cause_invalid_risk_becomes_unknown():
    text = '{"root_cause": "x", "risk": "EXTREME"}'
    assert parse_root_cause(text)["risk"] == "UNKNOWN"


def test_parse_root_cause_rejects_missing_root_cause():
    with pytest.raises(ValueError):
        parse_root_cause('{"evidence": "y"}')


def test_parse_root_cause_rejects_garbage():
    with pytest.raises(ValueError):
        parse_root_cause("not json at all")


# ------------------------------------------------------------------
# parse_patch
# ------------------------------------------------------------------

def test_parse_patch_valid():
    # identical old/new is rejected, so use distinct content
    text = json.dumps({
        "patch": [
            {"old_lines": ["count <= 8'h01;"], "new_lines": ["count <= 8'h00;"]},
        ]
    })
    patch = parse_patch(text)
    assert len(patch) == 1
    assert patch[0]["old_lines"] == ["count <= 8'h01;"]


def test_parse_patch_rejects_empty_and_non_list():
    with pytest.raises(ValueError):
        parse_patch('{"patch": []}')
    with pytest.raises(ValueError):
        parse_patch('{"patch": "bad"}')


def test_parse_patch_rejects_same_old_and_new():
    text = json.dumps({"patch": [{"old_lines": ["x"], "new_lines": ["x"]}]})
    with pytest.raises(ValueError):
        parse_patch(text)


# ------------------------------------------------------------------
# patch application
# ------------------------------------------------------------------

def test_count_occurrences():
    assert count_occurrences("a a a", "a") == 3
    assert count_occurrences("ab ab", "ab") == 2
    assert count_occurrences("aa", "aaa") == 0
    assert count_occurrences("x", "") == 0


def test_validate_patch_unique_match_ok():
    text = "l1\ncount <= 8'h01;\nl3\n"
    patch = [{"old_lines": ["count <= 8'h01;"], "new_lines": ["count <= 8'h00;"]}]
    validate_patch(text, patch)  # should not raise


def test_validate_patch_missing_raises():
    with pytest.raises(PatchError):
        validate_patch("l1\nl2\n", [{"old_lines": ["missing line"], "new_lines": ["n"]}])


def test_validate_patch_ambiguous_raises():
    with pytest.raises(PatchError):
        validate_patch("dup\ndup\n", [{"old_lines": ["dup"], "new_lines": ["new"]}])


def test_apply_patch_to_text_replaces_exactly_once():
    text = "line1\nreset <= 8'h01;\nline3\n"
    patch = [{"old_lines": ["reset <= 8'h01;"], "new_lines": ["reset <= 8'h00;"]}]
    new_text = apply_patch_to_text(text, patch)
    assert new_text == "line1\nreset <= 8'h00;\nline3\n"


def test_apply_patch_to_file_creates_timestamped_backup(tmp_path):
    rtl = tmp_path / "counter.v"
    rtl.write_text("reset <= 8'h01;\n", encoding="utf-8")
    patch = [{"old_lines": ["reset <= 8'h01;"], "new_lines": ["reset <= 8'h00;"]}]

    reports_dir = tmp_path / "reports"
    new_text, backup_path = apply_patch_to_file(
        rtl, patch, reports_dir=reports_dir,
        timestamp=datetime(2026, 11, 21, tzinfo=timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
    )

    assert rtl.read_text(encoding="utf-8") == "reset <= 8'h00;\n"
    assert Path(backup_path).exists()
    assert Path(backup_path).read_text(encoding="utf-8") == "reset <= 8'h01;\n"


# ------------------------------------------------------------------
# debug report + email body
# ------------------------------------------------------------------

def test_build_debug_report_contains_sections():
    report = build_debug_report(
        rtl_file="counter.v",
        tb_file="tb_counter.v",
        error_reason="Compilation failed",
        before={"compile_status": "FAIL", "simulation_status": "NOT_RUN"},
        root_cause={"root_cause": "bad reset", "evidence": "line 12", "risk": "HIGH"},
        patch=[{"old_lines": ["a"], "new_lines": ["b"]}],
        approval_status="applied",
        backup_path="reports/backups/counter-20261121T000000Z.bak",
        after={"compile_status": "PASS", "simulation_status": "PASS"},
        mail_status="Email sent successfully to x@y.com",
        timestamp="20261121T000000Z",
    )
    assert "RTL Debug & Fix Report" in report
    assert "Compilation failed" in report
    assert "bad reset" in report
    assert "HIGH" in report
    assert "Before:" in report and "a" in report
    assert "Email sent successfully" in report


def test_build_debug_email_body_contains_report_and_footer():
    body = build_debug_email_body("# RTL Debug & Fix Report\nhello")
    assert body.startswith("# RTL Debug & Fix Report")
    assert "debug-fix stage" in body
