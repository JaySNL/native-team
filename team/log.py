"""Render a raw tmux pipe-pane tee into something readable.

Ink redraws the entire frame on every tick, so the raw tee is ~96% escape
codes and spinner frames. Measured: 341KB in, 13.9KB of unique content.

The dedupe is global (a seen set over the whole file), not consecutive-only.
This is deliberate: Ink's frame redraws interleave with spinner lines, so
identical content lines recur non-consecutively and a consecutive-only collapse
would not remove them. The cost is real: if a grunt legitimately prints the
same line twice (e.g. the same source line quoted in two different answers),
the second occurrence is dropped. We accept that to get 341 KB down to 13.9 KB.

The rendered output is guaranteed to contain zero \\x1b, \\x9b, and zero \\x07
bytes. After ANSI stripping, any surviving \\x1b or \\x9b (8-bit CSI) is a
truncated or unrecognized sequence; such lines are dropped entirely as torn or
untrustworthy redraw artifacts. This is correct for a diagnostic transcript and
prevents the output from corrupting a terminal.
"""
import re

ANSI = re.compile(
    r"\x1b\[[0-9;?]*[A-Za-z]"      # CSI (SGR, cursor, erase)
    r"|\x1b\][^\x07\x1b\n]*(?:\x07|\x1b\\)"  # OSC (no newlines in body), BEL or ST-terminated
    r"|\x9b[0-9;?]*[A-Za-z]"       # 8-bit CSI (C1 equivalent of ESC [)
    r"|\x1b[()][AB012]"             # charset selection
    r"|\x1b[=>]"                    # keypad mode
)
# qwen renders elapsed time as "7.5s", "2m", "2m 15s", "1h 2m 3s". The
# original pattern only matched seconds, so a real 1.1 MB capture came
# back still full of spinner frames.
SPINNER = re.compile(r"\((?:\d+(?:\.\d+)?[hms]\s*)+·[^)]*esc to cancel\)")
# In a narrow pane qwen wraps the spinner across two terminal lines:
#     .. Why did the developer go broke? ... (2m 1s . 740 tokens .
#                                             esc to cancel)
# Neither half matches SPINNER -- the head has no ")", the tail no elapsed
# time. Both are matched per line, so neither can swallow real content.
SPINNER_HEAD = re.compile(r"\((?:\d+(?:\.\d+)?[hms]\s*)+·[^)]*·\s*$")
SPINNER_TAIL = re.compile(r"^[\s.]*esc to cancel\)\s*$")


def render(raw: str) -> str:
    text = ANSI.sub("", raw).replace("\r", "\n")

    # Drop lines with residual escape sequences (torn or unrecognized redraws).
    # Any surviving \x1b or \x9b is a truncated or unrecognized sequence;
    # drop the entire line rather than truncating it, since it's untrustworthy.
    # Also remove any stray \x07 (BEL) bytes from surviving lines.
    text_lines = []
    for line in text.split("\n"):
        # Drop lines with residual escapes; they are artifacts or torn writes.
        if "\x1b" in line or "\x9b" in line:
            continue
        # Strip stray BEL bytes from surviving lines.
        line = line.replace("\x07", "")
        text_lines.append(line)

    seen: set[str] = set()
    out: list[str] = []
    for line in text_lines:
        line = line.rstrip()
        if not line.strip():
            continue
        if (SPINNER.search(line) or SPINNER_HEAD.search(line)
                or SPINNER_TAIL.match(line)):
            continue
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
    return "\n".join(out)
