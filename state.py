"""
state.py

Global verification state.
"""

verification_state = {

    "rtl_file": None,
    "tb_file": None,

    "module_info": {},

    "simulation_result": {},

    "log_analysis": {},

    "rtl_analysis": {},

    "tb_analysis": {},

    "missing_edge_cases": [],

    "suggestions": [],

    "final_report": ""
}


def reset_state():
    """
    Reset state before a new verification run.
    """

    verification_state.clear()

    verification_state.update({

        "rtl_file": None,
        "tb_file": None,

        "module_info": {},

        "simulation_result": {},

        "log_analysis": {},

        "rtl_analysis": {},

        "tb_analysis": {},

        "missing_edge_cases": [],

        "suggestions": [],

        "final_report": ""
    })