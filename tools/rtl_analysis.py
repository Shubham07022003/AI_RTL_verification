"""
tools/rtl_analysis.py

Static RTL Analysis + AI Explanation
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


def extract_rtl_findings(rtl_code: str):

    findings = []

   
    # Multiple count updates


    count_updates = len(
        re.findall(
            r"count\s*<=",
            rtl_code
        )
    )

    if count_updates > 1:
        findings.append(
            "Register 'count' updated multiple times."
        )

    # Multiple state updates
   

    state_updates = len(
        re.findall(
            r"state\s*<=",
            rtl_code,
            flags=re.IGNORECASE
        )
    )

    if state_updates > 1:
        findings.append(
            "FSM state updated multiple times."
        )


    # Simultaneous read/write risk
   

    if (
        "wr_en" in rtl_code and
        "rd_en" in rtl_code and
        "count <=" in rtl_code
    ):
        findings.append(
            "Possible simultaneous read/write count conflict."
        )

   
    # Missing reset
   

    if not re.search(
        r"\brst\b|\breset\b",
        rtl_code,
        flags=re.IGNORECASE
    ):
        findings.append(
            "No reset logic detected."
        )

    # FSM without default
   

    if "case" in rtl_code.lower():

        if "default" not in rtl_code.lower():

            findings.append(
                "Case statement without default."
            )

   
    # Latch risk
  

    if (
        "always @(*)" in rtl_code or
        "always_comb" in rtl_code
    ):

        if "else" not in rtl_code:

            findings.append(
                "Possible latch inference."
            )


    # Width mismatch risk
   

    if "integer" in rtl_code:

        findings.append(
            "Integer registers detected. Verify width requirements."
        )

    return findings


@tool
def analyze_rtl() -> str:
    """
    Static RTL Analysis + AI Review
    """

    simulation_result = verification_state.get(
        "simulation_result",
        {}
    )

    # if simulation_result.get(
    #     "compile_status"
    # ) != "PASS":

    #     return (
    #         "RTL Analysis Skipped.\n"
    #         "Compilation failed."
    #     )

    if simulation_result.get("compile_status") != "PASS":
        # Let the AI read the compile log and explain the error instead of skipping!
        compile_log_path = simulation_result.get("compile_log")
        log_content = Path(compile_log_path).read_text()
        return f"Compilation failed. Analyze these errors: {log_content}"

    rtl_file = verification_state.get(
        "rtl_file"
    )

    rtl_code = Path(
        rtl_file
    ).read_text(
        encoding="utf-8",
        errors="ignore"
    )

    findings = extract_rtl_findings(
        rtl_code
    )

    # ----------------------------------
    # No findings
    # ----------------------------------

    if not findings:

        result = """
RTL Issues:
No obvious RTL issues detected.

Risk Level:
LOW
"""

        verification_state["rtl_analysis"] = {
            "analysis": result,
            "findings": []
        }

        return result

    # ----------------------------------
    # AI Explanation
    # ----------------------------------

    prompt = f"""
You are a Senior RTL Verification Engineer.

RTL CODE
========
{rtl_code}

STATIC FINDINGS
===============
{chr(10).join(findings)}

Rules:

1. Explain ONLY the findings listed.
2. Do NOT invent new bugs.
3. Do NOT report FIFO pointer wraparound.
4. Do NOT report circular buffer behavior.
5. Do NOT report style issues.

Return:

RTL Issues:

Issue:
...

Evidence:
...

Impact:
...

Recommendation:
...

Risk Level:
LOW/MEDIUM/HIGH
"""

    analysis_result = invoke_llm(
        prompt,
        temperature=0
    )

    verification_state["rtl_analysis"] = {
        "analysis": analysis_result,
        "findings": findings
    }

    return analysis_result