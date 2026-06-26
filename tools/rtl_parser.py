"""
tools/rtl_parser.py

RTL Parser Tool

Extract:
- Module Name
- Parameters
- Inputs
- Outputs
- Inouts
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
def rtl_parser() -> str:
    """
    Parse RTL file and extract module information.

    Returns:
        Summary of extracted RTL information.
    """

    # rtl_file = verification_state["rtl_file"]
    rtl_file = verification_state.get("rtl_file")

    if not rtl_file:
        return "ERROR: RTL file path not found in state."

    rtl_path = Path(rtl_file)

    if not rtl_path.exists():
        return f"ERROR: RTL file not found: {rtl_file}"

    rtl_code = rtl_path.read_text(
        encoding="utf-8",
        errors="ignore"
    )

  
    # Module Name
   

    module_match = re.search(
        r"\bmodule\s+(\w+)",
        rtl_code,
        re.MULTILINE
    )

    module_name = (
        module_match.group(1)
        if module_match
        else "UNKNOWN"
    )

    # Parameters
    

    parameters = re.findall(
        r"\bparameter\s+(\w+)",
        rtl_code
    )


    # Inputs
   

    inputs = re.findall(
        r"\binput\b\s*(?:wire|reg|logic)?\s*(?:\[[^\]]+\])?\s*(\w+)",
        rtl_code
    )

   
    # Outputs
   

    outputs = re.findall(
        r"\boutput\b\s*(?:wire|reg|logic)?\s*(?:\[[^\]]+\])?\s*(\w+)",
        rtl_code
    )

   
    # Inouts


    inouts = re.findall(
        r"\binout\b\s*(?:wire)?\s*(?:\[[^\]]+\])?\s*(\w+)",
        rtl_code
    )


    # Store Result
   

    module_info = {
        "module_name": module_name,
        "parameters": parameters,
        "inputs": inputs,
        "outputs": outputs,
        "inouts": inouts,
        "rtl_file": rtl_file
    }

    verification_state["module_info"] = module_info

   
    # Agent Summary


    summary = f"""
RTL Parsing Complete

Module Name:
{module_name}

Inputs:
{inputs}

Outputs:
{outputs}

Inouts:
{inouts}

Parameters:
{parameters}
"""

    return summary.strip()