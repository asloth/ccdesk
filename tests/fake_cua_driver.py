#!/usr/bin/env python3
"""A tiny stdio MCP driver for protocol, process-lifetime and failure tests."""
import json
import os
import sys
import time

if "--version" in sys.argv:
    print("cua-driver " + os.environ.get("FAKE_VERSION", "0.38.0"))
    sys.exit()

for line in sys.stdin:
    request = json.loads(line)
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(json.dumps({"pid": os.getpid(), **request}) + "\n")
    if "id" not in request:
        continue
    if request["method"] == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake-cua", "version": "0.38.0"}}
    else:
        name = request["params"]["name"]
        if name == "check_permissions":
            structured = {"xsend_event": True, "atspi": os.environ.get("FAKE_ATSPI") != "0", "wayland_enabled": True,
                          "background_available": os.environ.get("FAKE_UNAVAILABLE") != "1"}
        elif name == "list_windows":
            structured = {"windows": json.loads(os.environ["FAKE_WINDOWS"])}
        else:
            mode = os.environ.get("FAKE_FAILURE")
            if mode == "exit":
                sys.exit(1)
            if mode == "timeout":
                time.sleep(60)
            structured = json.loads(mode) if mode else {"ok": True}
        result = {"content": [], "structuredContent": structured,
                  "isError": structured.get("ok") is False}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
