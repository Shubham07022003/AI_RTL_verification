"""
Tests for UVM support: detection, facts extraction, the UVM backend,
dispatch, plain/UVM analysis awareness, and the debug-fix integration.

No real UVM simulator or Groq access is needed - stub runners and fake
LLMs are used throughout.
"""

import json
from pathlib import Path

import pytest

import error_fix
import state as state_module
from state import verification_state
from tools.uvm import detect_testbench_type, extract_uvm_facts, is_uvm_tb
from tools.uvm_backend import (
    build_uvm_command,
    run_uvm_simulation,
    _resolve_uvm_config,
)
from tools.simulation import compile_and_simulate
from tools.tb_checker import check_uvm_testbench
import tools.tb_analysis as tb_analysis


REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "example" / "counter_uvm_tb.v"

PLAIN_TB = """
module tb_plain;
  initial begin
    $display("start");
    #10 $finish;
  end
endmodule
"""

UVM_TB = """
import uvm_pkg::*;

class counter_env extends uvm_env;
    `uvm_component_utils(counter_env)

    virtual counter_if vif;

    virtual function void build_phase(uvm_phase phase);
        super.build_phase(phase);
        vif = counter_if::type_id::create("vif", this);
    endfunction
endclass

class counter_test extends uvm_test;
    `uvm_component_utils(counter_test)
    counter_env env;

    virtual function void build_phase(uvm_phase phase);
        super.build_phase(phase);
        env = counter_env::type_id::create("env", this);
    endfunction
endclass

class counter_sequence extends uvm_sequence #(counter_item);
    `uvm_object_utils(counter_sequence)
    task body();
    endtask
endclass

module tb_counter;
    counter_test top;
endmodule
"""


# ------------------------------------------------------------------
# Detection
# ------------------------------------------------------------------

def test_is_uvm_tb_positive():
    assert is_uvm_tb(UVM_TB)
    assert is_uvm_tb("`uvm_component_utils(x);")
    assert is_uvm_tb("class env extends uvm_env;")
    assert is_uvm_tb("class t extends uvm_test;")


def test_is_uvm_tb_negative():
    assert not is_uvm_tb(PLAIN_TB)
    assert not is_uvm_tb("")
    assert not is_uvm_tb("module tb; initial begin end endmodule")


def test_is_uvm_tb_ignores_comments():
    commented = "// class fake extends uvm_test\nmodule tb; endmodule"
    assert not is_uvm_tb(commented)
    block_commented = "/* class fake extends uvm_test */\nmodule tb; endmodule"
    assert not is_uvm_tb(block_commented)


def test_detect_testbench_type(tmp_path):
    tb = tmp_path / "tb_uvm.v"
    tb.write_text(UVM_TB)
    assert detect_testbench_type(tb) == "UVM"

    tb = tmp_path / "tb.v"
    tb.write_text(PLAIN_TB)
    assert detect_testbench_type(tb) == "PLAIN"

    assert detect_testbench_type(tmp_path / "missing.v") == "UNKNOWN"


# ------------------------------------------------------------------
# Facts extraction
# ------------------------------------------------------------------

def test_extract_uvm_facts_inline():
    facts = extract_uvm_facts(UVM_TB)
    assert facts["testbench_type"] == "UVM"
    assert facts["top_module"] == "tb_counter"
    assert facts["top_test_class"] == "counter_test"
    assert "uvm_env" in facts["components_extended"]
    assert "uvm_test" in facts["components_extended"]
    assert "uvm_sequence" in facts["components_extended"]
    assert "build_phase" in facts["phases_overridden"]
    assert facts["sequences_count"] == 1
    assert facts["uvm_config_db"] is False  # not used in the inline sample


def test_extract_uvm_facts_on_example():
    facts = extract_uvm_facts(EXAMPLE.read_text(encoding="utf-8"), EXAMPLE)
    assert facts["testbench_type"] == "UVM"
    assert facts["top_module"] == "tb_counter"
    assert facts["top_test_class"] == "counter_test"
    assert "uvm_env" in facts["components_extended"]
    assert "uvm_test" in facts["components_extended"]
    assert "uvm_sequence" in facts["components_extended"]
    assert "uvm_sequence_item" not in facts["components_extended"]
    assert "build_phase" in facts["phases_overridden"]
    assert "run_phase" in facts["phases_overridden"]
    assert facts["uvm_config_db"] is True
    assert facts["scoreboard_present"] is True
    assert facts["monitor_present"] is True
    assert facts["sequencer_present"] is True
    assert facts["dut_module"] == "counter"
    assert facts["dut_instance"] == "dut"


def test_extract_uvm_facts_on_example_detects_all_uvm_macros():
    facts = extract_uvm_facts(EXAMPLE.read_text(encoding="utf-8"), EXAMPLE)
    assert facts["top_extends"] is None  # example top module has no extends clause
    assert facts["driver_present"] is True


# ------------------------------------------------------------------
# Backend
# ------------------------------------------------------------------

def test_build_uvm_command_substitutes_placeholders():
    cmd = build_uvm_command(
        "vcs -sverilog -l {TEMP_DIR}/log.log {RTL} {TB}",
        "/a/counter.v", "/b/tb.v", "/tmp/out",
    )
    assert cmd == "vcs -sverilog -l /tmp/out/log.log /a/counter.v /b/tb.v"


def test_resolve_uvm_config_single_mode(monkeypatch):
    monkeypatch.setenv("UVM_SIM_CMD", "vcs -sverilog {RTL} {TB}")
    monkeypatch.setenv("UVM_COMPILE_CMD", "")
    monkeypatch.setenv("UVM_RUN_CMD", "")
    config = _resolve_uvm_config()
    assert config["mode"] == "single"
    assert config["command"] == "vcs -sverilog {RTL} {TB}"


def test_resolve_uvm_config_two_step_mode(monkeypatch):
    monkeypatch.delenv("UVM_SIM_CMD", raising=False)
    monkeypatch.setenv("UVM_COMPILE_CMD", "vlog {RTL} {TB}")
    monkeypatch.setenv("UVM_RUN_CMD", "vsim {TB}")
    config = _resolve_uvm_config()
    assert config["mode"] == "two-step"
    assert config["compile_command"] == "vlog {RTL} {TB}"
    assert config["run_command"] == "vsim {TB}"


def test_resolve_uvm_config_misconfigured(monkeypatch):
    monkeypatch.delenv("UVM_SIM_CMD", raising=False)
    monkeypatch.setenv("UVM_COMPILE_CMD", "vlog {TB}")
    monkeypatch.delenv("UVM_RUN_CMD", raising=False)
    assert _resolve_uvm_config()["mode"] == "misconfigured"


def test_resolve_uvm_config_none(monkeypatch):
    for var in ("UVM_SIM_CMD", "UVM_COMPILE_CMD", "UVM_RUN_CMD"):
        monkeypatch.delenv(var, raising=False)
    assert _resolve_uvm_config() is None


def test_run_uvm_simulation_single_step_success(tmp_path, monkeypatch):
    monkeypatch.setenv("UVM_SIM_CMD", "vcs -sverilog {RTL} {TB}")

    def fake_runner(command, timeout):
        return 0, "SIMULATION FINISHED NORMALLY"

    result = run_uvm_simulation(
        tmp_path / "counter.v", tmp_path / "tb.v",
        temp_dir=tmp_path / "temp", _runner=fake_runner,
    )
    assert result["compile_status"] == "PASS"
    assert result["simulation_status"] == "PASS"
    assert "SIMULATION FINISHED" in result["compile_output"]
    assert (tmp_path / "temp" / "simulation.log").exists()


def test_run_uvm_simulation_single_step_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("UVM_SIM_CMD", "vcs {RTL} {TB}")

    def fake_runner(command, timeout):
        return 1, "ERROR: compilation failed"

    result = run_uvm_simulation(
        tmp_path / "counter.v", tmp_path / "tb.v",
        temp_dir=tmp_path / "temp", _runner=fake_runner,
    )
    assert result["compile_status"] == "FAIL"
    assert result["simulation_status"] == "FAIL"


def test_run_uvm_simulation_two_step_stops_on_compile_failure(tmp_path, monkeypatch):
    monkeypatch.delenv("UVM_SIM_CMD", raising=False)
    monkeypatch.setenv("UVM_COMPILE_CMD", "vlog {RTL} {TB}")
    monkeypatch.setenv("UVM_RUN_CMD", "vsim {TB}")
    calls = []

    def fake_runner(command, timeout):
        calls.append(command)
        return (1, "sim error") if "vlog" in command else (0, "simv complete")

    result = run_uvm_simulation(
        tmp_path / "counter.v", tmp_path / "tb.v",
        temp_dir=tmp_path / "temp", _runner=fake_runner,
    )
    # compile failed -> run step never invoked
    assert calls == [f"vlog {tmp_path}/counter.v {tmp_path}/tb.v"]
    assert result["compile_status"] == "FAIL"
    assert result["simulation_status"] == "NOT_RUN"


def test_run_uvm_simulation_two_step_full_success(tmp_path, monkeypatch):
    monkeypatch.delenv("UVM_SIM_CMD", raising=False)
    monkeypatch.setenv("UVM_COMPILE_CMD", "vlog {RTL} {TB}")
    monkeypatch.setenv("UVM_RUN_CMD", "vsim {TB}")

    def fake_runner(command, timeout):
        return 0, "simv PASS"

    result = run_uvm_simulation(
        tmp_path / "counter.v", tmp_path / "tb.v",
        temp_dir=tmp_path / "temp", _runner=fake_runner,
    )
    assert result["compile_status"] == "PASS"
    assert result["simulation_status"] == "PASS"


def test_run_uvm_simulation_unconfigured(tmp_path, monkeypatch):
    for var in ("UVM_SIM_CMD", "UVM_COMPILE_CMD", "UVM_RUN_CMD"):
        monkeypatch.delenv(var, raising=False)

    result = run_uvm_simulation(
        tmp_path / "counter.v", tmp_path / "tb.v",
        temp_dir=tmp_path / "temp",
    )
    assert result["compile_status"] == "FAIL"
    assert "no UVM simulator is configured" in result["compile_output"]


def test_run_uvm_simulation_misconfigured_two_step(tmp_path, monkeypatch):
    monkeypatch.delenv("UVM_SIM_CMD", raising=False)
    monkeypatch.setenv("UVM_COMPILE_CMD", "vlog {TB}")
    monkeypatch.delenv("UVM_RUN_CMD", raising=False)

    result = run_uvm_simulation(
        tmp_path / "counter.v", tmp_path / "tb.v",
        temp_dir=tmp_path / "temp",
    )
    assert result["compile_status"] == "FAIL"
    assert "BOTH UVM_COMPILE_CMD and UVM_RUN_CMD" in result["compile_output"]


# ------------------------------------------------------------------
# Dispatch: compile_and_simulate routes plain vs UVM
# ------------------------------------------------------------------

def test_compile_and_simulate_routes_to_uvm_backend(tmp_path, monkeypatch):
    """UVM TB file -> UVM backend (echo stub -> PASS)."""
    tb = tmp_path / "counter_uvm_tb.v"
    tb.write_text(UVM_TB)

    monkeypatch.setenv("UVM_SIM_CMD", 'echo "UVM SIMULATION COMPLETE"')

    result = compile_and_simulate(
        tmp_path / "counter.v", tb, temp_dir=tmp_path / "temp",
    )
    assert result["compile_status"] == "PASS"
    assert result["simulation_status"] == "PASS"
    assert "UVM SIMULATION COMPLETE" in result["compile_output"]


def test_compile_and_simulate_plain_path_unchanged(tmp_path, monkeypatch):
    """Plain TB file -> plain Icarus path (fails gracefully without iverilog)."""
    tb = tmp_path / "tb_counter.v"
    tb.write_text(PLAIN_TB)
    result = compile_and_simulate(
        tmp_path / "counter.v", tb, temp_dir=tmp_path / "temp",
    )
    # default test environment has no iverilog -> FAIL
    assert result["compile_status"] == "FAIL"


# ------------------------------------------------------------------
# check_uvm_testbench
# ------------------------------------------------------------------

def test_check_uvm_testbench_on_uvm_tb(tmp_path, monkeypatch):
    tb = tmp_path / "counter_uvm_tb.v"
    tb.write_text(UVM_TB)
    saved = dict(verification_state)
    verification_state.clear()
    verification_state["tb_file"] = str(tb)
    try:
        summary = check_uvm_testbench()
    finally:
        verification_state.clear()
        verification_state.update(saved)

    assert "UVM Testbench Facts" in summary
    assert "uvm_env" in summary
    assert "build_phase" in summary


def test_check_uvm_testbench_rejects_plain_tb(tmp_path, monkeypatch):
    tb = tmp_path / "tb.v"
    tb.write_text(PLAIN_TB)
    saved = dict(verification_state)
    verification_state.clear()
    verification_state["tb_file"] = str(tb)
    try:
        result = check_uvm_testbench()
    finally:
        verification_state.clear()
        verification_state.update(saved)

    assert "Not a UVM testbench" in result


# ------------------------------------------------------------------
# tb_analysis UVM prompt injection
# ------------------------------------------------------------------

def test_analyze_testbench_injects_uvm_facts_in_prompt(tmp_path, monkeypatch):
    rtl = tmp_path / "counter.v"
    tb = tmp_path / "counter_uvm_tb.v"
    rtl.write_text("module counter; endmodule\n")
    tb.write_text(UVM_TB)

    captured = {}
    def fake_llm(prompt, temperature=0.0, **kwargs):
        captured["prompt"] = prompt
        return "Covered Scenarios:\n- x\n\nMissing Scenarios:\n- y\n\nMissing Edge Cases:\n- z\n\nTestbench Weaknesses:\n- w\n\nRecommendations:\n- r\n\nCoverage Confidence:\nLOW"

    monkeypatch.setattr(tb_analysis, "invoke_llm", fake_llm)

    saved = dict(verification_state)
    verification_state.clear()
    verification_state["rtl_file"] = str(rtl)
    verification_state["tb_file"] = str(tb)
    verification_state["module_info"] = {"module_name": "counter", "inputs": [], "outputs": []}
    verification_state["simulation_result"] = {"compile_status": "PASS", "simulation_status": "PASS"}
    verification_state["testbench_type"] = "UVM"
    try:
        tb_analysis.analyze_testbench()
    finally:
        verification_state.clear()
        verification_state.update(saved)

    assert "UVM TESTBENCH FACTS" in captured["prompt"]
    assert "UVM CRITICAL RULES" in captured["prompt"]
    assert "uvm_env" in captured["prompt"]


def test_analyze_testbench_plain_tb_has_no_uvm_block(tmp_path, monkeypatch):
    rtl = tmp_path / "counter.v"
    tb = tmp_path / "tb_counter.v"
    rtl.write_text("module counter; endmodule\n")
    tb.write_text(PLAIN_TB)

    captured = {}
    def fake_llm(prompt, temperature=0.0, **kwargs):
        captured["prompt"] = prompt
        return "Covered Scenarios:\n- x\n\nMissing Scenarios:\n- y\n\nMissing Edge Cases:\n- z\n\nTestbench Weaknesses:\n- w\n\nRecommendations:\n- r\n\nCoverage Confidence:\nLOW"

    monkeypatch.setattr(tb_analysis, "invoke_llm", fake_llm)

    saved = dict(verification_state)
    verification_state.clear()
    verification_state["rtl_file"] = str(rtl)
    verification_state["tb_file"] = str(tb)
    verification_state["module_info"] = {"module_name": "counter", "inputs": [], "outputs": []}
    verification_state["simulation_result"] = {"compile_status": "PASS", "simulation_status": "PASS"}
    verification_state["testbench_type"] = "PLAIN"
    try:
        tb_analysis.analyze_testbench()
    finally:
        verification_state.clear()
        verification_state.update(saved)

    assert "UVM TESTBENCH FACTS" not in captured["prompt"]


# ------------------------------------------------------------------
# Debug-fix integration with UVM (fake LLM)
# ------------------------------------------------------------------

def test_debug_fix_with_uvm_reruns_through_uvm_backend(tmp_path, monkeypatch):
    """Full flow: detection -> fake LLM -> patch -> rerun via UVM backend."""
    tb = tmp_path / "counter_uvm_tb.v"
    tb.write_text(UVM_TB)
    rtl = tmp_path / "counter.v"
    rtl.write_text("module counter;\n    count <= 8'h01;\nendmodule\n")

    # deterministic rerun: echo the command (fake a successful sim)
    monkeypatch.setenv("UVM_SIM_CMD", 'echo "UVM SIM: PASS"')

    FAKE_RC = json.dumps({
        "root_cause": "reset value wrong",
        "evidence": "count <= 8'h01;",
        "risk": "HIGH",
    })
    FAKE_PATCH = json.dumps({
        "patch": [
            {"old_lines": ["    count <= 8'h01;"], "new_lines": ["    count <= 8'h00;"]}
        ]
    })

    def fake_llm(prompt, temperature=0.0, max_tokens=2048):
        if "minimal code patch" in prompt:
            return FAKE_PATCH
        return FAKE_RC

    monkeypatch.setattr(error_fix, "invoke_llm", fake_llm)
    monkeypatch.delenv("EMAIL_SENDER", raising=False)
    monkeypatch.delenv("EMAIL_PASSWORD", raising=False)
    monkeypatch.delenv("EMAIL_RECEIVER", raising=False)

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
    verification_state["log_analysis"] = {"compile_error_count": 1, "simulation_error_count": 0}
    verification_state["rtl_analysis"] = {"analysis": "", "findings": ["No reset logic detected."]}
    verification_state["tb_analysis"] = {"analysis": ""}
    verification_state["testbench_type"] = "UVM"
    verification_state["uvm_facts"] = {"testbench_type": "UVM", "top_module": "tb_counter"}
    verification_state["missing_edge_cases"] = []
    verification_state["final_report"] = ""

    monkeypatch.chdir(tmp_path)
    try:
        result = error_fix.run_debug_fix(auto_approve=True, send_email=False, timeout=10)
    finally:
        verification_state.clear()
        verification_state.update(saved)

    assert result["status"] == "applied"
    assert "8'h00" in rtl.read_text(encoding="utf-8")
    # the rerun should have gone through the UVM backend (echo stub)
    assert result["rerun"]["compile_status"] == "PASS"
    assert result["rerun"]["simulation_status"] == "PASS"
    assert (tmp_path / "reports" / "debug_report.md").exists()
