# """
# tools/tb_analysis.py

# AI-powered Testbench Analysis Tool
# """

# import re
# from pathlib import Path

# try:
#     from langchain.tools import tool
# except Exception:
#     def tool(func):
#         return func

# from state import verification_state
# from llm import invoke_llm

# # -----------------------------------------
# # Analysis LLM
# # -----------------------------------------
# @tool
# def analyze_testbench() -> str:
#     """
#     Analyze RTL and Testbench.

#     Detect:
#     - Missing edge cases
#     - Weak stimulus
#     - Missing reset testing
#     - Missing corner cases
#     - Missing assertions
#     """

#     simulation_result = verification_state.get(
#         "simulation_result",
#         {}
#     )

#     if simulation_result.get("compile_status") != "PASS":
#         return (
#             "Testbench Analysis Skipped.\n"
#             "Reason: RTL/Testbench compilation failed."
#         )

#     rtl_file = verification_state.get("rtl_file")
#     tb_file = verification_state.get("tb_file")

#     if not rtl_file:
#         return "RTL file not found."

#     if not tb_file:
#         return "Testbench file not found."

#     rtl_code = Path(
#         rtl_file
#     ).read_text(
#         encoding="utf-8",
#         errors="ignore"
#     )

#     tb_code = Path(
#         tb_file
#     ).read_text(
#         encoding="utf-8",
#         errors="ignore"
#     )

#     module_info = verification_state.get(
#         "module_info",
#         {}
#     )

#     rtl_analysis = verification_state.get(
#         "rtl_analysis",
#         {}
#     )

#     # -----------------------------------------
#     # Static TB Analysis
#     # -----------------------------------------

#     tb_lower = tb_code.lower()

#     has_assertions = (
#         "assert" in tb_lower
#     )

#     has_scoreboard = (
#         "scoreboard" in tb_lower
#     )

#     has_monitor = (
#         "$monitor" in tb_lower
#     )

#     has_display = (
#         "$display" in tb_lower
#     )

#     has_write = (
#         "wr_en" in tb_lower
#     )

#     has_read = (
#         "rd_en" in tb_lower
#     )

#     has_reset = (
#         "rst" in tb_lower
#     )

#     has_random = (
#         "$random" in tb_lower or
#         "$urandom" in tb_lower
#     )

#     has_repeat = (
#         "repeat" in tb_lower
#     )

#     has_simultaneous_rw = (
#         "wr_en = 1" in tb_lower and
#         "rd_en = 1" in tb_lower
#     )

#     has_overflow_test = (
#         "full" in tb_lower and
#         (
#             "if(full" in tb_lower or
#             "wait(full" in tb_lower or
#             "@(posedge full" in tb_lower
#         )
#     )

#     has_underflow_test = (
#         "empty" in tb_lower and
#         (
#             "if(empty" in tb_lower or
#             "wait(empty" in tb_lower or
#             "@(posedge empty" in tb_lower
#         )
#     )

#     tb_facts = f"""
# TB FACTS
# ========

# Reset Present:
# {has_reset}

# Write Stimulus Present:
# {has_write}

# Read Stimulus Present:
# {has_read}

# Assertions Present:
# {has_assertions}

# Scoreboard Present:
# {has_scoreboard}

# Monitor Present:
# {has_monitor}

# Display Present:
# {has_display}

# Random Stimulus Present:
# {has_random}

# Repeat Loops Present:
# {has_repeat}

# Overflow Test Present:
# {has_overflow_test}

# Underflow Test Present:
# {has_underflow_test}

# Simultaneous Read/Write Present:
# {has_simultaneous_rw}
# """

#     # -----------------------------------------
#     # Prompt
#     # -----------------------------------------

#     prompt = f"""
# You are a Senior RTL Verification Engineer.

# Your job is to evaluate whether
# the testbench sufficiently verifies
# the RTL functionality.

# =================================================
# RTL MODULE INFO
# =================================================

# {module_info}

# =================================================
# RTL ANALYSIS
# =================================================

# {rtl_analysis}

# =================================================
# SIMULATION RESULT
# =================================================

# {simulation_result}

# =================================================
# TESTBENCH FACTS
# =================================================

# {tb_facts}

# =================================================
# RTL CODE
# =================================================

# {rtl_code}

# =================================================
# TESTBENCH CODE
# =================================================

# {tb_code}

# =================================================
# STRICT RULES
# =================================================

# 1. Use ONLY evidence from RTL and TB.
# 2. NEVER invent missing scenarios.
# 3. NEVER suggest tests already present.
# 4. Do NOT report parameter sweep tests.
# 5. Do NOT report coding style issues.
# 6. Focus only on verification coverage.

# =================================================
# VERIFICATION CHECKLIST
# =================================================

# Check whether TB verifies:

# - Reset behavior
# - Basic write operation
# - Basic read operation
# - Full condition
# - Empty condition
# - Overflow behavior
# - Underflow behavior
# - Simultaneous read/write
# - Boundary conditions
# - Pointer wraparound
# - Data correctness
# - Assertions
# - Self-checking
# - Scoreboard

# =================================================
# OUTPUT FORMAT
# =================================================

# Covered Scenarios:
# - item

# Missing Scenarios:
# - item

# Missing Edge Cases:
# - item

# Testbench Weaknesses:
# - item

# Recommendations:
# - item

# Coverage Confidence:
# LOW/MEDIUM/HIGH
# """

#     analysis_result = invoke_llm(
#         prompt,
#         temperature=0
#     )

#     # ----------------------------------
#     # Extract Missing Edge Cases
#     # ----------------------------------

#     missing_edge_cases = []

#     collect = False

#     for line in analysis_result.splitlines():

#         text = line.strip()

#         normalized = re.sub(
#             r"[*_`#:\s]",
#             "",
#             text
#         ).lower()

#         if normalized.startswith(
#             "missingedgecases"
#         ):
#             collect = True
#             continue

#         if collect:

#             section_break = (
#                 "testbenchweaknesses",
#                 "recommendations",
#                 "coverageconfidence",
#                 "missingscenarios",
#                 "coveredscenarios",
#             )

#             if any(
#                 normalized.startswith(item)
#                 for item in section_break
#             ):
#                 break

#             bullet_match = re.match(
#                 r"^[-*]\s+(.*)$",
#                 text
#             )

#             numbered_match = re.match(
#                 r"^\d+[\.)]\s+(.*)$",
#                 text
#             )

#             if bullet_match:
#                 missing_edge_cases.append(
#                     bullet_match.group(1).strip()
#                 )

#             elif numbered_match:
#                 missing_edge_cases.append(
#                     numbered_match.group(1).strip()
#                 )

#     # ----------------------------------
#     # Save State
#     # ----------------------------------

#     verification_state["tb_analysis"] = {
#         "analysis": analysis_result,
#         "has_assertions": has_assertions,
#         "has_scoreboard": has_scoreboard,
#         "has_reset": has_reset,
#         "has_write": has_write,
#         "has_read": has_read,
#         "has_random": has_random,
        
#     }

#     verification_state[
#         "missing_edge_cases"
#     ] = missing_edge_cases

#     return analysis_result


























































"""
tools/tb_analysis.py
AI-powered Testbench Analysis Tool (Dynamic Version)
"""

import re
from pathlib import Path
from state import verification_state
from tools.uvm import detect_testbench_type, extract_uvm_facts

try:
    from langchain.tools import tool
except Exception:
    def tool(func):
        return func

try:
    from llm import invoke_llm
except Exception:
    invoke_llm = None


@tool
def analyze_testbench() -> str:
    """
    Analyze RTL and Testbench. Detects missing edge cases and coverage 
    dynamically based on module ports.
    """
    sim_result = verification_state.get("simulation_result", {})
    # if sim_result.get("compile_status") != "PASS":
    #     return "Testbench Analysis Skipped. Reason: Compilation failed."

    if sim_result.get("compile_status") != "PASS":
        # Let the AI read the compile log and explain the error instead of skipping!
        compile_log_path = sim_result.get("compile_log")
        try:
            log_content = Path(compile_log_path).read_text(encoding="utf-8", errors="ignore") if compile_log_path else "(no compile log available)"
        except Exception as e:
            log_content = f"(could not read compile log: {e})"
        return f"Compilation failed. Analyze these errors: {log_content}"
    rtl_file = verification_state.get("rtl_file")
    tb_file = verification_state.get("tb_file")
    module_info = verification_state.get("module_info", {})
    rtl_analysis = verification_state.get("rtl_analysis", {})

    rtl_code = Path(rtl_file).read_text(encoding="utf-8", errors="ignore")
    tb_code = Path(tb_file).read_text(encoding="utf-8", errors="ignore")
    tb_lower = tb_code.lower()

    # ---------------------------------------------------------
    # UVM DETECTION + FACTS
    # ---------------------------------------------------------
    testbench_type = verification_state.get("testbench_type") or detect_testbench_type(tb_file)
    is_uvm = (testbench_type == "UVM")
    uvm_facts = verification_state.get("uvm_facts") or (
        extract_uvm_facts(tb_code, tb_file) if is_uvm else {}
    )

    # ---------------------------------------------------------
    # DYNAMIC CHECKLIST GENERATION
    # ---------------------------------------------------------
    outputs = module_info.get("outputs", [])
    
    # Create a dynamic checklist based on actual ports
    dynamic_checklist = [f"Verification of output port '{port}'" for port in outputs]
    dynamic_checklist.append("Reset sequence and behavior")
    dynamic_checklist.append("Input data corner cases/ranges")
    dynamic_checklist.append("Invalid/Illegal input combinations")
    
    checklist_str = "\n".join([f"- {item}" for item in dynamic_checklist])

    # ---------------------------------------------------------
    # UVM FACTS BLOCK (only for UVM testbenches)
    # ---------------------------------------------------------
    uvm_block = ""
    if is_uvm:
        uvm_block = f"""
    =================================================
    UVM TESTBENCH FACTS (extracted deterministically)
    =================================================
    Testbench Type: UVM
    Top Module: {uvm_facts.get('top_module')}
    Top Test Class: {uvm_facts.get('top_test_class')}
    Top Test Module Extends: {uvm_facts.get('top_extends')}
    Components Extended: {uvm_facts.get('components_extended')}
    Phases Overridden: {uvm_facts.get('phases_overridden')}
    Sequences: {uvm_facts.get('sequences_count')}
    uvm_config_db Used: {uvm_facts.get('uvm_config_db')}
    Scoreboard Present: {uvm_facts.get('scoreboard_present')}
    Monitor Present: {uvm_facts.get('monitor_present')}
    Driver Present: {uvm_facts.get('driver_present')}
    Sequencer Present: {uvm_facts.get('sequencer_present')}
    Assertions Present: {uvm_facts.get('assertions_present')}
    DUT Module: {uvm_facts.get('dut_module')} (instance: {uvm_facts.get('dut_instance')})
    =================================================

    UVM CRITICAL RULES - follow strictly:
    - This is a UVM testbench. It uses uvm_test/uvm_env/uvm_sequencer/uvm_driver/uvm_monitor/uvm_scoreboard
      classes and phases instead of plain Verilog $display/$monitor/rst/wr_en/empty/full idioms.
    - Do NOT flag the absence of those plain-Verilog mechanisms as a weakness or missing coverage.
    - Evaluate the UVM structure: components present, sequences, phase overrides, config_db, scoreboard.
    - Only flag missing items when no UVM evidence could provide them (e.g. no scoreboard/monitor/driver at all).
    """

    # ---------------------------------------------------------
    # PROMPT
    # ---------------------------------------------------------
    prompt = f"""
    You are a Senior RTL Verification Engineer. 
    Analyze the provided testbench and RTL to determine if the design is fully verified.

    RTL Module: {module_info.get('module_name')}
    Inputs: {module_info.get('inputs')}
    Outputs: {outputs}

    =================================================
    VERIFICATION CHECKLIST (Verify these)
    =================================================
    {checklist_str}
    - Assertions / Self-checking mechanisms
    - Scoreboard implementation

    {uvm_block}

    =================================================
    STRICT RULES
    =================================================
    1. Use ONLY evidence from the provided RTL and TB code.
    2. Do NOT mention FIFO, Pointers, or Buffers unless they exist in the module info.
    3. Focus on the actual ports: {module_info.get('inputs') + outputs}.
    {"4. This is a UVM testbench - follow the UVM critical rules above. Never apply plain-Verilog checks to it." if is_uvm else ""}
    
    =================================================
    OUTPUT FORMAT
    =================================================
    Covered Scenarios:
    - (list)

    Missing Scenarios:
    - (list)

    Missing Edge Cases:
    - (list)

    Testbench Weaknesses:
    - (list)

    Recommendations:
    - (list)

    Coverage Confidence:
    LOW/MEDIUM/HIGH
    """

    if invoke_llm is None:
        return "Testbench analysis unavailable: llm module not importable (groq missing)."

    analysis_result = invoke_llm(prompt, temperature=0)

    # ---------------------------------------------------------
    # EXTRACT MISSING EDGE CASES
    # ---------------------------------------------------------
    missing_edge_cases = []
    collect = False
    for line in analysis_result.splitlines():
        text = line.strip()
        normalized = re.sub(r"[*_`#:\s]", "", text).lower()
        
        if normalized.startswith("missingedgecases"):
            collect = True
            continue
        if collect:
            if any(normalized.startswith(s) for s in ["testbenchweaknesses", "recommendations", "coverageconfidence"]):
                break
            if re.match(r"^[-*]\s+(.*)$", text) or re.match(r"^\d+[\.)]\s+(.*)$", text):
                missing_edge_cases.append(re.sub(r"^[-*]\d+[\.)]\s+", "", text).strip())

    # ---------------------------------------------------------
    # SAVE STATE
    # ---------------------------------------------------------
    verification_state["testbench_type"] = testbench_type
    verification_state["uvm_facts"] = uvm_facts
    verification_state["tb_analysis"] = {"analysis": analysis_result}
    verification_state["missing_edge_cases"] = missing_edge_cases

    return analysis_result