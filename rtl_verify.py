"""
rtl_verify.py

Usage:
    python rtl_verify.py <rtl_file> <tb_file>
    python rtl_verify.py fifo.v tb_fifo.v --yes

Options:
    --yes, -y      Auto-approve the proposed RTL patch (no prompt).
    --no-fix       Skip the debug-fix stage entirely.
    --no-email     Do not send the debug report by email.
"""

import argparse
import os
import sys
from pathlib import Path

from state import verification_state, reset_state
from tools.uvm import detect_testbench_type, extract_uvm_facts


def validate_file(filepath):
    """
    Check if file exists.
    """
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="AI-powered RTL Verification Agent",
    )
    parser.add_argument("rtl_file", help="Path to the RTL design file (.v/.sv)")
    parser.add_argument("tb_file", help="Path to the testbench file (.v/.sv)")
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Auto-approve the proposed RTL patch (required for non-interactive runs)",
    )
    parser.add_argument(
        "--no-fix",
        action="store_true",
        help="Skip the debug-fix stage (root-cause analysis / patch / rerun)",
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Do not send the debug report by email",
    )
    return parser


def main():
    args = build_arg_parser().parse_args()

    rtl_file = args.rtl_file
    tb_file = args.tb_file

    # Validate files
    try:
        validate_file(rtl_file)
        validate_file(tb_file)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Reset and populate the shared state
    reset_state()
    verification_state["rtl_file"] = os.path.abspath(rtl_file)
    verification_state["tb_file"] = os.path.abspath(tb_file)

    # Detect the testbench type and (for UVM) extract the structured facts
    tb_text = Path(verification_state["tb_file"]).read_text(encoding="utf-8", errors="ignore")
    testbench_type = detect_testbench_type(verification_state["tb_file"])
    verification_state["testbench_type"] = testbench_type

    if testbench_type == "UVM":
        verification_state["uvm_facts"] = extract_uvm_facts(tb_text, verification_state["tb_file"])

    print("\n===================================")
    print(" RTL Verification Agent Started")
    print("===================================")

    print(f"RTL File : {rtl_file}")
    print(f"TB File  : {tb_file}")
    print(f"Testbench Type : {testbench_type}")

    if testbench_type == "UVM":
        facts = verification_state["uvm_facts"]
        print("UVM Facts:")
        for key, value in facts.items():
            print(f"  {key}: {value}")

    try:
        from agent import run_verification
        report = run_verification()

        print("\n===================================")
        print(" Verification Complete")
        print("===================================\n")

        print(report)

    except Exception as e:
        print("\nVerification Failed")
        print(f"Error: {str(e)}")
        sys.exit(1)

    # ------------------------------------------
    # Debug-fix stage: root-cause analysis, patch approval,
    # patch application, simulation rerun, debug report/email
    # ------------------------------------------

    if not args.no_fix:
        print("\n===================================")
        print(" Debug-Fix Stage")
        print("===================================")
        try:
            from error_fix import run_debug_fix
            run_debug_fix(
                auto_approve=args.yes,
                send_email=not args.no_email,
            )
        except Exception as e:
            print(f"Debug-fix stage failed: {e}")
            sys.exit(1)


if __name__ == "__main__":
    main()
