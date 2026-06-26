"""
agent.py

Simple RTL verification orchestration.
Runs tools in a fixed order and returns the final report text.
"""

import os

from dotenv import load_dotenv
from tools.mail_sender import send_email_report
from state import verification_state
from tools.log_analyzer import analyze_logs
from tools.rtl_analysis import analyze_rtl
from tools.rtl_parser import rtl_parser
from tools.simulation import run_simulation
from tools.tb_analysis import analyze_testbench
from tools.report import generate_report

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY not found in environment.")


def _invoke_tool(tool_obj, name: str) -> str:
    """Invoke a LangChain tool with no arguments and return a text summary."""
    try:
        if hasattr(tool_obj, "invoke"):
            return tool_obj.invoke({})
        return tool_obj()
    except Exception as exc:
        raise RuntimeError(f"{name} failed: {exc}") from exc


def run_verification() -> str:
    """
    Run RTL verification steps in the expected order and return final report text.
    """
    # Ensure files were initialized by rtl_verify.py
    if not verification_state.get("rtl_file"):
        raise ValueError("RTL file path not set in verification state.")
    if not verification_state.get("tb_file"):
        raise ValueError("Testbench file path not set in verification state.")

   
    steps = [
    (rtl_parser, "rtl_parser"),
    (run_simulation, "run_simulation"),
    (analyze_logs, "analyze_logs"),
    (analyze_rtl, "analyze_rtl"),
    (analyze_testbench, "analyze_testbench"),
    (generate_report, "generate_report"),
    (send_email_report, "send_email_report")
    ]
    

    step_summaries = []
    for tool_obj, name in steps:
        output = _invoke_tool(tool_obj, name)
        step_summaries.append(f"[{name}]\n{output}")

    final_report = verification_state.get("final_report")
    if final_report:
        return final_report

    # Fallback should never happen if generate_report succeeds.
    return "\n\n".join(step_summaries)

