import mimetypes

APP_NAME = "Kaloscope"
"""The name of the application."""

ENCODING = "utf-8"
"""The default encoding for strings and files."""

URL_PREFIX = "/_api"
"""The URL prefix to be prepended to all routes."""

MCP_PATH = f"{URL_PREFIX}/mcp"
"""The only endpoint accepting dedicated MCP access tokens."""

SESSION_ID = "KSID"
"""The key for the session ID in the HTTP cookies."""

NFO_MIME_TYPE = "text/x-nfo"
"""The MIME type for NFO files."""

# register the NFO MIME type, which may be absent from the system's MIME type database
mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
