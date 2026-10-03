"""Stable tool failures shared by dispatch, file operations, and processes."""


NEXT_ACTIONS = {
    "PERMISSION_DENIED": "Respect read-only mode; ask the user to change permissions if the task requires writes or commands.",
    "INVALID_ARGUMENT": "Correct the arguments before calling again.",
    "UNKNOWN_TOOL": "Use a tool from the advertised tool list.",
    "PATH_DENIED": "Use an allowed workspace path; do not bypass the restriction.",
    "NOT_FOUND": "Inspect the workspace and correct the path.",
    "FILE_EXISTS": "Read the existing file before deciding how to edit it.",
    "UNSUPPORTED_FILE": "Select a supported UTF-8 text file.",
    "EDIT_CONFLICT": "Read the affected file again before preparing a new edit.",
    "PATCH_CONTEXT_MISMATCH": "Read the affected file again and regenerate the patch.",
    "APPROVAL_DENIED": "Respect the denial; do not retry through another tool.",
    "APPROVAL_FAILED": "Resolve the approval failure before requesting the action again.",
    "COMMAND_DENIED": "Respect the command restriction; do not bypass it.",
    "JOB_LIMIT": "Wait for or cancel an existing job before starting another.",
    "UNKNOWN_JOB": "Inspect actual state; do not replay the command automatically.",
    "INVALID_OFFSET": "Use a returned next_offset or tail_start_offset.",
    "COMMAND_TIMEOUT": "Inspect output and side effects before deciding whether to run again.",
    "COMMAND_CANCELLED": "Respect the cancellation and inspect any partial effects.",
    "COMMAND_FAILED": "Inspect the exit code and output before correcting the command or project.",
    "EXECUTION_FAILED": "Inspect actual state before deciding whether to run again.",
    "OUTPUT_CAPTURE_FAILED": "Output may be incomplete; inspect actual state before acting.",
    "PARTIAL_WRITE": "Read all affected files before retrying; some changes were already applied.",
    "IO_ERROR": "Inspect filesystem state and permissions before trying again.",
    "OUTCOME_UNKNOWN": "Inspect actual state; do not replay automatically.",
}


def tool_failure(code: str, message: str, **extra) -> dict:
    # No current failure promises that replaying the same action is safe.
    return {"ok": False, "error_code": code, "error": message,
            "retryable": False, "next_action": NEXT_ACTIONS[code], **extra}


class ToolError(ValueError):
    def __init__(self, message: str, code: str = "INVALID_ARGUMENT"):
        super().__init__(message)
        self.code = code
