"""Build identity shared with native capture evidence."""

def tool_identity() -> dict:
    from ..solidworks import tool_record

    return tool_record()
