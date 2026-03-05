"""Stub tools for the smolagents guard layer demo.

Four tools mimicking a personal assistant: search, read, write, send.
"""

from smolagents import tool


@tool
def web_search(query: str) -> str:
    """Search the web and return result snippets.

    Args:
        query: The search query string.
    """
    return f"[Result 1] Wikipedia: {query}\n[Result 2] Blog post about {query}"


@tool
def read_file(filename: str) -> str:
    """Read a file from the knowledge base.

    Args:
        filename: Name of the file to read.
    """
    return f"Contents of {filename}: (stub data)"


@tool
def write_file(filename: str, content: str) -> str:
    """Write content to a file in the knowledge base (destructive).

    Args:
        filename: Name of the file to write.
        content: The content to write.
    """
    return f"Saved '{filename}' ({len(content)} chars)."


@tool
def send_message(recipient: str, body: str) -> str:
    """Send a message to a recipient on Slack (destructive, requires approval).

    Args:
        recipient: The recipient channel or user.
        body: The message body.
    """
    return f"Message sent to {recipient}: {body[:50]}..."
