"""
tools/tb_analysis.py

AI-powered Testbench Analysis Tool
"""

import re
from pathlib import Path

try:
    from langchain.tools import tool
except Exception:
    def tool(func):
        return func

from state import verification_state
from llm import invoke_llm

# -----------------------------------------
# Analysis LLM
# -----------------------------------------
@tool
def analyze_testbench() -> str:
    """
    Analyze RTL and Testbench.

    Detect:
    - Missing edge cases
    - Weak stimulus
    - Missing reset testing
    - Missing corner cases
    - Missing assertions
    """

    simulation_result = verification_state.get(
        "simulation_result",
        {}
    )

    if simulation_result.get("compile_status") != "PASS":
        return (
            "Testbench Analysis Skipped.\n"
            "Reason: RTL/Testbench compilation failed."
        )

    rtl_file = verification_state.get("rtl_file")
    tb_file = verification_state.get("tb_file")

    if not rtl_file:
        return "RTL file not found."

    if not tb_file:
        return "Testbench file not found."

    rtl_code = Path(
        rtl_file
    ).read_text(
        encoding="utf-8",
        errors="ignore"
    )

    tb_code = Path(
        tb_file
    ).read_text(
        encoding="utf-8",
        errors="ignore"
    )

    module_info = verification_state.get(
        "module_info",
        {}
    )

    rtl_analysis = verification_state.get(
        "rtl_analysis",
        {}
    )

    # -----------------------------------------
    # Static TB Analysis
    # -----------------------------------------

    tb_lower = tb_code.lower()

    has_assertions = (
        "assert" in tb_lower
    )

    has_scoreboard = (
        "scoreboard" in tb_lower
    )

    has_monitor = (
        "$monitor" in tb_lower
    )

    has_display = (
        "$display" in tb_lower
    )

    has_write = (
        "wr_en" in tb_lower
    )

    has_read = (
        "rd_en" in tb_lower
    )

    has_reset = (
        "rst" in tb_lower
    )

    has_random = (
        "$random" in tb_lower or
        "$urandom" in tb_lower
    )

    has_repeat = (
        "repeat" in tb_lower
    )

    has_simultaneous_rw = (
        "wr_en = 1" in tb_lower and
        "rd_en = 1" in tb_lower
    )

    has_overflow_test = (
        "full" in tb_lower and
        (
            "if(full" in tb_lower or
            "wait(full" in tb_lower or
            "@(posedge full" in tb_lower
        )
    )

    has_underflow_test = (
        "empty" in tb_lower and
        (
            "if(empty" in tb_lower or
            "wait(empty" in tb_lower or
            "@(posedge empty" in tb_lower
        )
    )

    tb_facts = f"""
TB FACTS
========

Reset Present:
{has_reset}

Write Stimulus Present:
{has_write}

Read Stimulus Present:
{has_read}

Assertions Present:
{has_assertions}

Scoreboard Present:
{has_scoreboard}

Monitor Present:
{has_monitor}

Display Present:
{has_display}

Random Stimulus Present:
{has_random}

Repeat Loops Present:
{has_repeat}

Overflow Test Present:
{has_overflow_test}

Underflow Test Present:
{has_underflow_test}

Simultaneous Read/Write Present:
{has_simultaneous_rw}
"""

    # -----------------------------------------
    # Prompt
    # -----------------------------------------

    prompt = f"""
You are a Senior RTL Verification Engineer.

Your job is to evaluate whether
the testbench sufficiently verifies
the RTL functionality.

=================================================
RTL MODULE INFO
=================================================

{module_info}

=================================================
RTL ANALYSIS
=================================================

{rtl_analysis}

=================================================
SIMULATION RESULT
=================================================

{simulation_result}

=================================================
TESTBENCH FACTS
=================================================

{tb_facts}

=================================================
RTL CODE
=================================================

{rtl_code}

=================================================
TESTBENCH CODE
=================================================

{tb_code}

=================================================
STRICT RULES
=================================================

1. Use ONLY evidence from RTL and TB.
2. NEVER invent missing scenarios.
3. NEVER suggest tests already present.
4. Do NOT report parameter sweep tests.
5. Do NOT report coding style issues.
6. Focus only on verification coverage.

=================================================
VERIFICATION CHECKLIST
=================================================

Check whether TB verifies:

- Reset behavior
- Basic write operation
- Basic read operation
- Full condition
- Empty condition
- Overflow behavior
- Underflow behavior
- Simultaneous read/write
- Boundary conditions
- Pointer wraparound
- Data correctness
- Assertions
- Self-checking
- Scoreboard

=================================================
OUTPUT FORMAT
=================================================

Covered Scenarios:
- item

Missing Scenarios:
- item

Missing Edge Cases:
- item

Testbench Weaknesses:
- item

Recommendations:
- item

Coverage Confidence:
LOW/MEDIUM/HIGH
"""

    analysis_result = invoke_llm(
        prompt,
        temperature=0
    )

    # ----------------------------------
    # Extract Missing Edge Cases
    # ----------------------------------

    missing_edge_cases = []

    collect = False

    for line in analysis_result.splitlines():

        text = line.strip()

        normalized = re.sub(
            r"[*_`#:\s]",
            "",
            text
        ).lower()

        if normalized.startswith(
            "missingedgecases"
        ):
            collect = True
            continue

        if collect:

            section_break = (
                "testbenchweaknesses",
                "recommendations",
                "coverageconfidence",
                "missingscenarios",
                "coveredscenarios",
            )

            if any(
                normalized.startswith(item)
                for item in section_break
            ):
                break

            bullet_match = re.match(
                r"^[-*]\s+(.*)$",
                text
            )

            numbered_match = re.match(
                r"^\d+[\.)]\s+(.*)$",
                text
            )

            if bullet_match:
                missing_edge_cases.append(
                    bullet_match.group(1).strip()
                )

            elif numbered_match:
                missing_edge_cases.append(
                    numbered_match.group(1).strip()
                )

    # ----------------------------------
    # Save State
    # ----------------------------------

    verification_state["tb_analysis"] = {
        "analysis": analysis_result,
        "has_assertions": has_assertions,
        "has_scoreboard": has_scoreboard,
        "has_reset": has_reset,
        "has_write": has_write,
        "has_read": has_read,
        "has_random": has_random,
        "has_overflow_test": has_overflow_test,
        "has_underflow_test": has_underflow_test,
        "has_simultaneous_rw": has_simultaneous_rw
    }

    verification_state[
        "missing_edge_cases"
    ] = missing_edge_cases

    return analysis_result