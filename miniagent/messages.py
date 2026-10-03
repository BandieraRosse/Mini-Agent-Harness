"""Explicit assistant phases, independent of tool presence or prose wording.

Chat Completions has no portable end-turn field. Prefixes provide a transport
fallback; adapters may also supply native phase/end_turn metadata.
"""

PHASES = ("commentary", "final_answer")
MARKERS = {f"[{phase}]": phase for phase in PHASES}


def classify(message, end_turn=None):
    message = dict(message)
    phase = message.get("phase")
    if phase is not None and phase not in PHASES:
        raise ValueError("Invalid assistant phase")
    if end_turn is not None and type(end_turn) is not bool:
        raise ValueError("Invalid end_turn signal")
    content = message.get("content")
    if isinstance(content, str):
        candidate = content.lstrip()
        for marker, marked_phase in MARKERS.items():
            if candidate.startswith(marker):
                if phase is not None and phase != marked_phase:
                    raise ValueError("Conflicting assistant phases")
                phase = marked_phase
                message["content"] = candidate[len(marker):].lstrip("\r\n ")
                break
    if end_turn is False:
        if phase == "final_answer":
            raise ValueError("Final answer conflicts with end_turn=false")
        phase = "commentary"
    elif end_turn is True:
        if phase == "commentary":
            raise ValueError("Commentary conflicts with end_turn=true")
        phase = "final_answer"
    if message.get("tool_calls"):
        if phase == "final_answer":
            raise ValueError("A final answer cannot contain executable tool calls")
        phase = "commentary"
    if message.get("refusal") and phase is None:
        phase = "final_answer"
    if phase is not None:
        message["phase"] = phase
    return message


class MessageDisplay:
    """Stream explicit commentary; hold candidate final/untagged text for admission."""

    def __init__(self, emit):
        self.emit = emit
        self.pending = ""
        self.commentary = False

    def feed(self, text):
        if self.commentary:
            self.emit(text)
            return
        self.pending += text
        candidate = self.pending.lstrip()
        marker = "[commentary]"
        if candidate.startswith(marker):
            self.commentary = True
            self.emit(candidate[len(marker):].lstrip("\r\n "))
            self.pending = ""

    def finish(self, message, *, admitted=True):
        if admitted and not self.commentary:
            content = message.get("content") or message.get("refusal")
            if content:
                self.emit(content)
