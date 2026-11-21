"""
Integration tests for the debug-fix stage (error_fix.py).

These run with a fake LLM (no network, no Groq, no LangChain, no Icarus).
The rerun simulation will fail gracefully (iverilog is absent in CI) -
nothing here asserts on the rerun status.
"""

import contextlib
import json
from pathlib import Path

import pytest

import error_fix
from state import verification_state


RTL = """module counter (
    input wire clk,
    input wire rst,
    output reg [7:0] count
);

    always @(posedge clk or posedge rst) begin
        if (rst)
            count <= 8'h01; // BUG: reset should be 0
        else
            count <= count + 1;
    end

endmodule
"""

FAKE_ROOT_CAUSE = json.dumps({
    "root_cause": "The reset value 8'h01 is wrong; count should reset to 0.",
    "evidence": "count <= 8'h01; (reset line)",
    "risk": "HIGH",
})

FAKE_PATCH = json.dumps({
    "patch": [
        {
            "old_lines": ["count <= 8'h01; // BUG: reset should be 0"],
            "new_lines": ["count <= 8'h00;"],
        }
    ]
})


def _fake_llm(prompt, temperature=0.0, max_tokens=2048):
    if "minimal code patch" in prompt:
        return FAKE_PATCH
    return FAKE_ROOT_CAUSE


@contextlib.contextmanager
def isolated(tmp_path, monkeypatch):
    """Tmp working dir with a planted RTL file + a pre-populated state."""
    monkeypatch.chdir(tmp_path)

    rtl = tmp_path / "counter.v"
    tb = tmp_path / "tb_counter.v"
    rtl.write_text(RTL, encoding="utf-8")
    tb.write_text("module tb_counter; endmodule\n", encoding="utf-8")

    saved = dict(verification_state)
    verification_state.clear()
    verification_state["rtl_file"] = str(rtl)
    verification_state["tb_file"] = str(tb)
    verification_state["module_info"] = {"module_name": "counter"}
    verification_state["simulation_result"] = {
        "compile_status": "FAIL",
        "simulation_status": "NOT_RUN",
        "compile_log": "temp/compile.log",
        "simulation_log": None,
    }
    verification_state["log_analysis"] = {
        "compile_error_count": 1,
        "simulation_error_count": 0,
        "detected_issues": ["Syntax Error"],
    }
    verification_state["rtl_analysis"] = {"analysis": "", "findings": []}
    verification_state["tb_analysis"] = {"analysis": ""}
    verification_state["missing_edge_cases"] = []
    verification_state["final_report"] = ""

    try:
        yield rtl, tb
    finally:
        verification_state.clear()
        verification_state.update(saved)


def test_debug_fix_full_flow_auto_approved(tmp_path, monkeypatch):
    """Patch auto-approved -> file patched, backup created, report written."""
    with isolated(tmp_path, monkeypatch) as (rtl, tb):
        monkeypatch.setattr(error_fix, "invoke_llm", _fake_llm)
        monkeypatch.delenv("EMAIL_SENDER", raising=False)
        monkeypatch.delenv("EMAIL_PASSWORD", raising=False)
        monkeypatch.delenv("EMAIL_RECEIVER", raising=False)

        result = error_fix.run_debug_fix(auto_approve=True, send_email=True, timeout=5)

    assert result["status"] == "applied"
    assert result["patch_applied"] is True
    # file was patched
    assert "count <= 8'h00;" in rtl.read_text(encoding="utf-8")
    assert "8'h01" not in rtl.read_text(encoding="utf-8")
    # backup was created with the ORIGINAL content
    backups = list((tmp_path / "reports" / "backups").glob("*.bak"))
    assert len(backups) == 1
    assert "8'h01" in backups[0].read_text(encoding="utf-8")
    # debug report written
    debug_report = tmp_path / "reports" / "debug_report.md"
    assert debug_report.exists()
    assert "Root-Cause Analysis" in debug_report.read_text(encoding="utf-8")
    # email reported as not-configured (nothing reached the network)
    assert result["mail_status"] == "Email configuration missing."


def test_debug_fix_skipped_when_no_error(tmp_path, monkeypatch):
    """No error in state -> stage skipped, no report written."""
    monkeypatch.chdir(tmp_path)

    saved = dict(verification_state)
    verification_state.clear()
    verification_state["rtl_file"] = str(tmp_path / "counter.v")
    verification_state["tb_file"] = str(tmp_path / "tb_counter.v")
    verification_state["simulation_result"] = {
        "compile_status": "PASS",
        "simulation_status": "PASS",
    }
    verification_state["log_analysis"] = {
        "compile_error_count": 0,
        "simulation_error_count": 0,
    }
    verification_state["rtl_analysis"] = {"findings": []}
    try:
        result = error_fix.run_debug_fix(auto_approve=True, send_email=False)
    finally:
        verification_state.clear()
        verification_state.update(saved)

    assert result["status"] == "skipped"
    assert result["debug_report_path"] is None
    assert not (tmp_path / "reports" / "debug_report.md").exists()


def test_debug_fix_declined_patch_not_applied(tmp_path, monkeypatch):
    """Non-interactive run without --yes -> patch NOT applied, but report saved."""
    with isolated(tmp_path, monkeypatch) as (rtl, tb):
        # non-TTY stdin (pytest) -> _wait_for_approval returns False
        monkeypatch.setattr(error_fix, "invoke_llm", _fake_llm)

        result = error_fix.run_debug_fix(auto_approve=False, send_email=False, timeout=5)

    assert result["status"] == "not_approved"
    assert rtl.read_text(encoding="utf-8") == RTL  # unchanged
    assert (tmp_path / "reports" / "debug_report.md").exists()


def test_debug_fix_invalid_patch_not_applied(tmp_path, monkeypatch):
    """An old_lines block that does not match fails validation -> not applied."""
    with isolated(tmp_path, monkeypatch) as (rtl, tb):
        bad_patch = json.dumps({
            "patch": [
                {
                    "old_lines": ["a line that definitely does not exist"],
                    "new_lines": ["x"],
                }
            ]
        })

        def fake_llm(prompt, temperature=0.0, max_tokens=2048):
            if "minimal code patch" in prompt:
                return bad_patch
            return FAKE_ROOT_CAUSE

        monkeypatch.setattr(error_fix, "invoke_llm", fake_llm)

        result = error_fix.run_debug_fix(auto_approve=True, send_email=False, timeout=5)

    assert result["status"] == "llm_error"
    assert result["patch_applied"] is False
    assert rtl.read_text(encoding="utf-8") == RTL  # unchanged


def test_debug_fix_no_llm(tmp_path, monkeypatch):
    """invoke_llm is None (groq missing) -> graceful llm_error, nothing applied."""
    with isolated(tmp_path, monkeypatch) as (rtl, tb):
        monkeypatch.setattr(error_fix, "invoke_llm", None)

        result = error_fix.run_debug_fix(auto_approve=True, send_email=False, timeout=5)

    assert result["status"] == "llm_error"
    assert "LLM is unavailable" in (result["llm_error"] or "")
    assert rtl.read_text(encoding="utf-8") == RTL
