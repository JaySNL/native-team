"""Shape validation. Pure: no filesystem, no tmux."""

MAX_BODY = 1000
MESSAGE_TYPES = frozenset({"result", "note", "blocked", "failed"})
RECORD_FIELDS = ("file", "line", "symbol", "evidence")
MESSAGE_FIELDS = ("id", "from", "type", "task", "body")


class SchemaError(Exception):
    pass


def validate_record(rec: dict) -> None:
    for key in RECORD_FIELDS:
        if key not in rec:
            raise SchemaError(f"record missing field: {key}")
    line = rec["line"]
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        raise SchemaError(f"line must be a positive int, got {line!r}")

    # Type-check all string fields
    for field_name in ("file", "symbol", "evidence"):
        if not isinstance(rec[field_name], str):
            raise SchemaError(f"{field_name} must be a string, got {type(rec[field_name]).__name__}")

    # Validate symbol is not empty after stripping
    if not rec["symbol"].strip():
        raise SchemaError("symbol cannot be empty; provide the actual symbol name")

    # Validate evidence is not empty after stripping
    if not rec["evidence"].strip():
        raise SchemaError("evidence is empty; quote the exact source line")

    # Validate symbol appears in evidence
    if rec["symbol"] not in rec["evidence"]:
        raise SchemaError(
            f"symbol {rec['symbol']!r} does not appear in evidence "
            f"{rec['evidence']!r} — a grep hit is not a verified citation"
        )


def validate_message(msg: dict) -> None:
    for key in MESSAGE_FIELDS:
        if key not in msg:
            raise SchemaError(f"message missing field: {key}")
    if not isinstance(msg["type"], str):
        raise SchemaError(f"type must be a string, got {type(msg['type']).__name__}")
    if msg["type"] not in MESSAGE_TYPES:
        raise SchemaError(
            f"unknown message type {msg['type']!r}; expected one of {sorted(MESSAGE_TYPES)}"
        )
    if not isinstance(msg["body"], str):
        raise SchemaError(f"body must be a string, got {type(msg['body']).__name__}")
    if len(msg["body"]) > MAX_BODY:
        raise SchemaError(
            f"body is {len(msg['body'])} chars; max {MAX_BODY}. "
            f"Write to a file and send a pointer."
        )
