"""
rtl_verify.py

Usage:
    python rtl_verify.py fifo.v tb_fifo.v
    python rtl_verify.py fifo.sv tb_fifo.sv
"""

import sys
import os

from state import verification_state


from agent import run_verification


def validate_file(filepath):
    """
    Check if file exists.
    """
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")


def main():
    verification_state.clear()
    # Check arguments
    if len(sys.argv) != 3:
        print(
            "\nUsage:\n"
            "python rtl_verify.py <rtl_file> <tb_file>\n"
            "\nExample:\n"
            "python rtl_verify.py fifo.v tb_fifo.v\n"
        )
        sys.exit(1)

    rtl_file = sys.argv[1]
    tb_file = sys.argv[2]

    # Validate files
    validate_file(rtl_file)
    validate_file(tb_file)

    # Save in global state
    verification_state["rtl_file"] = os.path.abspath(rtl_file)
    verification_state["tb_file"] = os.path.abspath(tb_file)

    print("\n===================================")
    print(" RTL Verification Agent Started")
    print("===================================")

    print(f"RTL File : {rtl_file}")
    print(f"TB File  : {tb_file}")

    try:

        report = run_verification()

        print("\n===================================")
        print(" Verification Complete")
        print("===================================\n")

        print(report)

    except Exception as e:

        print("\nVerification Failed")
        print(f"Error: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()