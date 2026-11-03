"""JSON-RPC 2.0 parsing and response helpers (MCP, no batching)."""
import json

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class JsonRpcError(Exception):
    def __init__(self, code, message, id=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.id = id


def parse(body: bytes) -> dict:
    def _reject_constant(name):
        raise ValueError(name)

    try:
        data = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise JsonRpcError(PARSE_ERROR, "Parse error") from None
    if isinstance(data, list):
        raise JsonRpcError(INVALID_REQUEST, "Batch requests are not supported")
    if not isinstance(data, dict):
        raise JsonRpcError(INVALID_REQUEST, "Invalid request")
    msg_id = data.get("id")
    if isinstance(msg_id, bool) or not (msg_id is None or isinstance(msg_id, (int, str))):
        raise JsonRpcError(INVALID_REQUEST, "Invalid id")
    if data.get("jsonrpc") != "2.0":
        raise JsonRpcError(INVALID_REQUEST, "jsonrpc must be '2.0'", msg_id)
    method = data.get("method")
    if not isinstance(method, str) or not method:
        raise JsonRpcError(INVALID_REQUEST, "method is required", msg_id)
    params = data.get("params")
    if params is not None and not isinstance(params, (dict, list)):
        raise JsonRpcError(INVALID_REQUEST, "params must be structured", msg_id)
    return data


def result(id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id, "result": result}


def error(id, code: int, message: str, data=None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id, "error": err}
