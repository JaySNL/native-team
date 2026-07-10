"""Render a raw tmux pipe-pane tee into something readable.

Ink redraws the entire frame on every tick, so the raw tee is ~96% escape
codes and spinner frames. Measured: 341KB in, 13.9KB of unique content.

The dedupe is global (a seen set over the whole file), not consecutive-only.
This is deliberate: Ink's frame redraws interleave with spinner lines, so
identical content lines recur non-consecutively and a consecutive-only collapse
would not remove them. The cost is real: if a grunt legitimately prints the
same line twice (e.g. the same source line quoted in two different answers),
the second occurrence is dropped. We accept that to get 341 KB down to 13.9 KB.
"""
import re

ANSI = re.compile(
    r"\x1b\[[0-9;?]*[A-Za-z]"      # CSI (SGR, cursor, erase)
    r"|\x1b\][^\x07]*\x07"          # OSC (window title), BEL-terminated
    r"|\x1b[()][AB012]"             # charset selection
    r"|\x1b[=>]"                    # keypad mode
)
SPINNER = re.compile(r"\(\d+(?:\.\d+)?s\s*·\s*esc to cancel\)")


def render(raw: str) -> str:
    text = ANSI.sub("", raw).replace("\r", "\n")
    seen: set[str] = set()
    out: list[str] = []
    for line in text.split("\n"):
        line = line.rstrip()
        if not line.strip():
            continue
        if SPINNER.search(line):
            continue
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
    return "\n".join(out)
