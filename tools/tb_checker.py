"""
tools/tb_checker.py

Rule-based Testbench Coverage Checker.
Provides factual coverage information
to reduce LLM hallucinations.
"""

import re
from pathlib import Path

try:
    from langchain.tools import tool
except Exception:
    def tool(func):
        return func

from state import verification_state
from tools.uvm import is_uvm_tb, extract_uvm_facts


@tool
def check_testbench() -> str:
    """
    Analyze testbench coverage using
    deterministic rules.
    """

    tb_file = verification_state.get("tb_file")

    if not tb_file:
        return "Testbench file not found."

    tb_code = Path(tb_file).read_text(
        encoding="utf-8",
        errors="ignore"
    )

    coverage = {}

    coverage["reset_test"] = bool(
        re.search(r"\brst\b", tb_code)
    )

    coverage["write_enable_used"] = bool(
        re.search(r"\bwr_en\b", tb_code)
    )

    coverage["read_enable_used"] = bool(
        re.search(r"\brd_en\b", tb_code)
    )

    coverage["monitor_present"] = (
        "$monitor" in tb_code
        or "$display" in tb_code
    )

    coverage["assertions_present"] = (
        "assert" in tb_code.lower()
    )

    coverage["scoreboard_present"] = any(
        keyword in tb_code.lower()
        for keyword in [
            "scoreboard",
            "expected",
            "queue",
            "reference_model"
        ]
    )

    coverage["self_checking"] = any(
        keyword in tb_code.lower()
        for keyword in [
            "if(",
            "if (",
            "!==",
            "===",
            "$fatal",
            "$error"
        ]
    )

    coverage["full_flag_checked"] = (
        "full" in tb_code
    )

    coverage["empty_flag_checked"] = (
        "empty" in tb_code
    )

    coverage["simultaneous_read_write"] = (
        re.search(
            r"wr_en\s*=.*1.*rd_en\s*=.*1",
            tb_code,
            re.DOTALL
        )
        is not None
    )

    verification_state["tb_checker"] = coverage

    summary = []

    summary.append("TB Coverage Facts")
    summary.append("================")

    for k, v in coverage.items():
        summary.append(
            f"{k}: {'YES' if v else 'NO'}"
        )

    return "\n".join(summary)

@tool
def check_uvm_testbench() -> str:
    """
    Rule-based UVM testbench coverage check.

    Produces deterministic UVM facts from the testbench source
    (component presence, phases overridden, sequences, config_db,
    scoreboard/monitor presence, DUT instantiation).
    """
    tb_file = verification_state.get("tb_file")

    if not tb_file:
        return "Testbench file not found."

    tb_code = Path(tb_file).read_text(
        encoding="utf-8",
        errors="ignore"
    )

    if not is_uvm_tb(tb_code):
        return "Not a UVM testbench - use check_testbench for plain Verilog testbenches."

    facts = extract_uvm_facts(tb_code, tb_file)
    verification_state["uvm_facts"] = facts
    verification_state["testbench_type"] = "UVM"

    coverage = {
        "top_test_class": facts.get("top_test_class"),
        "components_extended": facts.get("components_extended", []),
        "phases_overridden": facts.get("phases_overridden", []),
        "sequences_count": facts.get("sequences_count", 0),
        "uvm_config_db": facts.get("uvm_config_db", False),
        "scoreboard_present": facts.get("scoreboard_present", False),
        "monitor_present": facts.get("monitor_present", False),
        "driver_present": facts.get("driver_present", False),
        "sequencer_present": facts.get("sequencer_present", False),
        "assertions_present": facts.get("assertions_present", False),
        "dut_module": facts.get("dut_module"),
        "dut_instance": facts.get("dut_instance"),
    }

    summary = []
    summary.append("UVM Testbench Facts")
    summary.append("===================")

    for key, value in coverage.items():
        summary.append(f"{key}: {value}")

    return "\n".join(summary)
