"""Build identity shared with native capture evidence."""

def tool_identity() -> dict:
    from ..runtime import tool_record

    return tool_record()
