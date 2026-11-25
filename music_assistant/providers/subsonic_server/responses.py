"""
Subsonic response envelope, rendered as XML, JSON or JSONP from one dict model.

Payloads are plain dicts. A scalar becomes an XML attribute, a dict becomes a child element,
a list becomes repeated child elements (or a JSON array) and the key ``value`` becomes the
element text. Like Navidrome, the XML leaves out empty strings, empty lists and empty objects
below the top level, while the JSON keeps them.
"""

from __future__ import annotations

import re
from typing import Any

from aiohttp import web

from music_assistant.helpers.json import json_dumps

from .constants import API_VERSION, ERR_GENERIC, XML_NAMESPACE

JSONP_CALLBACK = re.compile(r"^[A-Za-z_$][\w$.]{0,63}$")
_XML_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


class SubsonicError(Exception):
    """A Subsonic API error, returned to the client inside the response envelope."""

    def __init__(self, code: int, message: str) -> None:
        """
        Initialize the error.

        :param code: The Subsonic error code.
        :param message: The message shown to the client.
        """
        super().__init__(message)
        self.code = code
        self.message = message


def envelope(server_type: str, server_version: str, status: str = "ok") -> dict[str, Any]:
    """
    Return the attributes every response starts with.

    :param server_type: The server type reported to clients.
    :param server_version: The server version reported to clients.
    :param status: "ok" or "failed".
    """
    return {
        "status": status,
        "version": API_VERSION,
        "type": server_type,
        "serverVersion": server_version,
        "openSubsonic": True,
    }


def render(
    head: dict[str, Any],
    payload: dict[str, Any] | None,
    fmt: str,
    callback: str | None = None,
) -> web.Response:
    """
    Render a response in the format the client asked for.

    :param head: The envelope attributes (see ``envelope``).
    :param payload: The response body, keyed by element name.
    :param fmt: The requested format: "xml", "json" or "jsonp".
    :param callback: The JSONP callback name.
    """
    body = {**head, **(payload or {})}
    if fmt == "json" or (fmt == "jsonp" and not callback):
        return web.Response(
            body=json_dumps({"subsonic-response": _clean_json(body)}).encode(),
            content_type="application/json",
        )
    if fmt == "jsonp" and callback:
        if not JSONP_CALLBACK.match(callback):
            callback = "callback"
        text = f"{callback}({json_dumps({'subsonic-response': _clean_json(body)})});"
        return web.Response(text=text, content_type="application/javascript")
    return web.Response(
        text=to_xml(body),
        content_type="application/xml",
        charset="utf-8",
    )


def render_error(
    head: dict[str, Any], error: SubsonicError, fmt: str, callback: str | None = None
) -> web.Response:
    """
    Render an error response (HTTP 200, status "failed").

    :param head: The envelope attributes, with status "failed".
    :param error: The error to report.
    :param fmt: The requested format.
    :param callback: The JSONP callback name.
    """
    return render(head, {"error": {"code": error.code, "message": error.message}}, fmt, callback)


def generic_error(message: str) -> SubsonicError:
    """
    Return a generic (code 0) error.

    :param message: The message shown to the client.
    """
    return SubsonicError(ERR_GENERIC, message)


def to_xml(body: dict[str, Any]) -> str:
    """
    Return the XML document for a response body.

    :param body: Envelope attributes plus payload.
    """
    # no XML declaration, like Navidrome
    parts: list[str] = []
    _write_element(parts, "subsonic-response", body, top=True)
    return "".join(parts)


def _write_element(parts: list[str], name: str, data: dict[str, Any], top: bool = False) -> None:
    """
    Append one element (with its attributes and children) to the output.

    :param parts: The output fragments.
    :param name: The element name.
    :param data: The element's attributes, children and text.
    :param top: Whether this is the root element.
    """
    attrs: list[str] = []
    if top:
        attrs.append(f'xmlns="{XML_NAMESPACE}"')
    children: list[str] = []
    text = ""
    for key, value in data.items():
        if value is None:
            continue
        if key == "value" and not isinstance(value, dict | list):
            text = _escape_text(_scalar(value))
            continue
        if isinstance(value, dict):
            if value or top:
                _write_element(children, key, value)
            continue
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict):
                    _write_element(children, key, entry)
                elif entry is not None:
                    children.append(f"<{key}>{_escape_text(_scalar(entry))}</{key}>")
            continue
        if value == "":
            continue
        attrs.append(f'{key}="{_escape_attr(_scalar(value))}"')
    attr_text = (" " + " ".join(attrs)) if attrs else ""
    parts.append(f"<{name}{attr_text}>{text}{''.join(children)}</{name}>")


def _scalar(value: Any) -> str:
    """
    Return the XML text for a scalar value.

    :param value: The value.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _escape_text(text: str) -> str:
    """
    Escape element text.

    :param text: The text.
    """
    text = _XML_INVALID.sub("", text)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attr(text: str) -> str:
    """
    Escape an attribute value.

    :param text: The value.
    """
    return _escape_text(text).replace('"', "&quot;").replace("\n", "&#10;").replace("\r", "&#13;")


def _clean_json(data: Any) -> Any:
    """
    Return the data without None values, for the JSON output.

    :param data: The data.
    """
    if isinstance(data, dict):
        return {k: _clean_json(v) for k, v in data.items() if v is not None}
    if isinstance(data, list):
        return [_clean_json(v) for v in data if v is not None]
    return data
