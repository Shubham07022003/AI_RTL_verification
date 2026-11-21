"""
error_fix.py

Debug-fix stage for the RTL Verification Agent.

When the verification pipeline detects an error in the RTL (compilation
failure, simulation failure, log errors, or static RTL findings), this
stage:

  1. Asks the LLM for a structured root-cause analysis (JSON).
  2. Asks the LLM for a minimal code patch (JSON).
  3. Prints the proposal and PAUSES for human approval
     (interactive y/N, or auto-approve with --yes).
  4. Applies the approved patch to the RTL file (with a timestamped
     backup), reruns the simulation, and saves a full debug report
     (reports/debug_report.md). The debug report is also sent by email
     when EMAIL_* configuration is present (unless --no-email).

The stage is deterministic CLI code (not an agent tool) so the human
approval prompt is reliable.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

from state import verification_state
from tools.simulation import compile_and_simulate
from tools.mail_sender import send_email_message
from patch_utils import (
    PatchError,
    apply_patch_to_file,
    build_debug_email_body,
    build_debug_report,
    detect_error_reason,
    parse_patch,
    parse_root_cause,
    validate_patch,
)

try:
    from llm import invoke_llm
except Exception:  # groq/llm not installed - keep the CLI importable
    invoke_llm = None


# ------------------------------------------------------------------
# Prompt builders
# ------------------------------------------------------------------

def _read_rtl_code(rtl_path):
    return Path(rtl_path).read_text(encoding="utf-8", errors="ignore")


def _read_log(path):
    if not path:
        return "(no log)"
    try:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
        return text if text.strip() else "(empty log)"
    except Exception as e:
        return f"(could not read log: {e})"


def _build_root_cause_prompt(state, rtl_code, reason):
    sim = state.get("simulation_result", {})
    findings = (state.get("rtl_analysis") or {}).get("findings") or []

    compile_log = _read_log(sim.get("compile_log"))
    sim_log = _read_log(sim.get("simulation_log"))

    return f"""You are a Senior RTL Verification Engineer. Analyze the evidence below and give a structured root-cause analysis of the RTL error.

ERROR REASON:
{reason}

COMPILE LOG:
{compile_log}

SIMULATION LOG:
{sim_log}

STATIC FINDINGS:
{findings if findings else "(none)"}

RTL CODE:
{rtl_code}

Return ONLY a JSON object (no prose, no markdown fences) with exactly this schema:
{{
  "root_cause": "<one paragraph: what is broken, why it produces the error, and which lines/file are involved>",
  "evidence": "<the exact log/code lines that prove the root cause>",
  "risk": "LOW|MEDIUM|HIGH"
}}"""


def _build_patch_prompt(state, rtl_code, reason, root_cause):
    # Numbered view of the file to help locate lines; the LLM must
    # return the exact file lines without any number prefix.
    numbered_code = "\n".join(
        f"{n:4d} | {line}" for n, line in enumerate(rtl_code.splitlines(), start=1)
    )

    return f"""You are a Senior RTL Verification Engineer. Based on the root-cause analysis, propose a minimal code patch to fix the RTL error.

ERROR REASON:
{reason}

ROOT CAUSE ANALYSIS:
{root_cause.get('root_cause', '(unavailable)')}

RTL CODE (line numbers are shown for your reference only - do NOT include them in old_lines):
{numbered_code}

Return ONLY a JSON object (no prose, no markdown fences) with exactly this schema:
{{
  "patch": [
    {{
      "old_lines": ["exact line from the RTL file, without any line number"],
      "new_lines": ["exact replacement line"]
    }}
  ]
}}

Rules:
- Change ONLY the lines needed to fix the error. Do NOT refactor, clean up, or change unrelated lines.
- old_lines must appear EXACTLY in the RTL file (same text, same whitespace) — they must match without any leading/trailing extra lines.
- If more than one block needs changing, include one entry per block.
- Do NOT invent changes unrelated to the error.
- Output the JSON object only."""


# ------------------------------------------------------------------
# LLM calls
# ------------------------------------------------------------------

def _ask_root_cause(state, rtl_code, reason, timeout):
    prompt = _build_root_cause_prompt(state, rtl_code, reason)
    text = invoke_llm(prompt, temperature=0, max_tokens=2048)
    return parse_root_cause(text)


def _ask_patch(state, rtl_code, reason, root_cause, timeout):
    prompt = _build_patch_prompt(state, rtl_code, reason, root_cause)
    text = invoke_llm(prompt, temperature=0, max_tokens=4096)
    return parse_patch(text)


# ------------------------------------------------------------------
# Approval
# ------------------------------------------------------------------

def _wait_for_approval(auto_approve):
    if auto_approve:
        return True
    if not sys.stdin.isatty():
        print("\n(Non-interactive session - pass --yes to auto-approve the patch.)")
        return False
    try:
        answer = input("\nApply this patch? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


def _print_patch_preview(root_cause, patch, rtl_file):
    print("\n" + "=" * 60)
    print(" PROPOSED ROOT-CAUSE ANALYSIS")
    print("=" * 60)
    print(f"Risk: {root_cause.get('risk', 'UNKNOWN')}")
    print(root_cause.get("root_cause", "(no root cause)"))
    if root_cause.get("evidence"):
        print("\nEvidence:")
        print(root_cause["evidence"])

    print("\n" + "=" * 60)
    print(f" PROPOSED PATCH ({len(patch)} block(s)) - {rtl_file}")
    print("=" * 60)
    for index, entry in enumerate(patch):
        print(f"\n--- Patch entry {index + 1}")
        print("BEFORE:")
        for line in entry["old_lines"]:
            print(f"  {line}")
        print("AFTER:")
        for line in entry["new_lines"]:
            print(f"  {line}")
    print()


# ------------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------------

def run_debug_fix(auto_approve=False, send_email=True, timeout=None):
    """
    Run the debug-fix stage.

    Args:
        auto_approve: if True, apply the patch without asking (--yes).
        send_email:   if False, do not send the debug report by email.
        timeout:      per-command timeout (seconds) used for the rerun simulation.

    Returns a dict describing the outcome.
    """
    state = verification_state

    report_path_holder = {"path": None}
    status = "skipped"
    approval_status = "skipped"

    trigger, reason = detect_error_reason(state)

    if not trigger:
        print("\nNo RTL error detected - debug-fix stage skipped.\n")
        return {"status": "skipped", "reason": "no error detected",
                "debug_report_path": None}

    rtl_file = state.get("rtl_file")
    tb_file = state.get("tb_file")

    if not rtl_file or not Path(rtl_file).exists():
        print(f"\nDebug-fix stage cannot find RTL file: {rtl_file}\n")
        return {"status": "error", "reason": f"RTL file not found: {rtl_file}",
                "debug_report_path": None}

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    before = {
        "compile_status": (state.get("simulation_result") or {}).get("compile_status"),
        "simulation_status": (state.get("simulation_result") or {}).get("simulation_status"),
        "compile_log": (state.get("simulation_result") or {}).get("compile_log"),
        "simulation_log": (state.get("simulation_result") or {}).get("simulation_log"),
    }

    print(f"\n{'=' * 60}")
    print(" RTL ERROR DETECTED")
    print("=" * 60)
    print(f"Reason: {reason}")

    llm_error = None
    root_cause = {"root_cause": "(root-cause analysis unavailable)", "evidence": "", "risk": "UNKNOWN"}
    patch = None

    # --------------------------------------------------------------
    # Step 1 & 2: structured root cause, then minimal patch
    # --------------------------------------------------------------
    if invoke_llm is None:
        llm_error = ("LLM is unavailable (groq/langchain not installed) - "
                     "cannot generate a root-cause analysis or patch.")
    else:
        try:
            rtl_code = _read_rtl_code(rtl_file)
            root_cause = _ask_root_cause(state, rtl_code, reason, timeout)
        except Exception as exc:
            llm_error = f"Root-cause analysis failed: {exc}"

        if llm_error is None:
            try:
                patch = _ask_patch(state, rtl_code, reason, root_cause, timeout)
                validate_patch(rtl_code, patch)  # must match the file before approval
            except Exception as exc:
                llm_error = f"Patch generation/validation failed: {exc}"
                patch = None

    # --------------------------------------------------------------
    # Step 3: human approval
    # --------------------------------------------------------------
    approved = False
    if patch is not None:
        _print_patch_preview(root_cause, patch, rtl_file)
        approved = _wait_for_approval(auto_approve)
        if not approved:
            print("\nPatch declined - nothing was changed.")
    else:
        print("\nCould not generate a valid patch.")
        if llm_error:
            print(f"Reason: {llm_error}")

    # --------------------------------------------------------------
    # Step 4: apply patch, rerun simulation, report
    # --------------------------------------------------------------
    backup_path = None
    rerun = None
    status = "not_approved"
    approval_status = "not_approved"

    if patch is not None and approved:
        try:
            _new_text, backup_path = apply_patch_to_file(rtl_file, patch, timestamp=ts)
            status = "applied"
            approval_status = "applied"
            try:
                rerun = compile_and_simulate(rtl_file, tb_file, timeout=timeout)
                state["simulation_result"] = rerun
            except Exception as exc:
                llm_error = f"Simulation rerun failed: {exc}"
        except PatchError as exc:
            status = "patch_apply_failed"
            approval_status = "patch_apply_failed"
            llm_error = f"Patch could not be applied: {exc}"
            print(f"\n{llm_error}\n")
    elif patch is not None and not approved:
        status = "not_approved"
        approval_status = "not_approved"
    else:
        status = "llm_error"
        approval_status = "llm_error"

    # --------------------------------------------------------------
    # Debug report + email
    # --------------------------------------------------------------
    after = {
        "compile_status": (rerun or {}).get("compile_status"),
        "simulation_status": (rerun or {}).get("simulation_status"),
        "compile_log": (rerun or {}).get("compile_log"),
        "simulation_log": (rerun or {}).get("simulation_log"),
    } if rerun else {}

    def _build_report_text(mail_status=None):
        text = build_debug_report(
            rtl_file=rtl_file,
            tb_file=tb_file,
            error_reason=reason,
            before=before,
            root_cause=root_cause,
            patch=patch,
            approval_status=approval_status,
            backup_path=backup_path,
            after=after,
            mail_status=mail_status,
            timestamp=ts,
        )
        if llm_error:
            text += f"\n## Failure Note\n\n{llm_error}\n"
        return text

    # Email body must not embed the email status itself, so trim it off
    report_text = _build_report_text()
    email_body_text = report_text
    email_marker = report_text.rfind("\n## Email")
    if email_marker != -1:
        email_body_text = report_text[:email_marker]

    mail_status = None
    if send_email:
        mail_status = send_email_message(
            f"RTL Debug & Fix Report - {Path(rtl_file).stem}",
            build_debug_email_body(email_body_text),
        )

    # Rewrite the report with the final email status
    report_text = _build_report_text(mail_status)

    reports_dir = Path("reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / "debug_report.md"
    report_path.write_text(report_text, encoding="utf-8")
    report_path_holder["path"] = str(report_path)

    print(f"\nDebug report saved: {report_path}")
    if send_email:
        print(f"Email: {mail_status}")
    else:
        print("Email: disabled (--no-email)")

    print(f"Status: {status}")

    return {
        "status": status,
        "reason": reason,
        "root_cause": root_cause,
        "patch_applied": status == "applied",
        "new_file": Path(rtl_file).name,
        "backup_path": str(backup_path) if backup_path else None,
        "rerun": rerun,
        "debug_report_path": report_path_holder["path"],
        "mail_status": mail_status,
        "llm_error": llm_error,
    }
