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