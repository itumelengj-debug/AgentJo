#!/usr/bin/env python3
"""Minimal line-JSON MCP server: echo, add, boom (isError), slow (2s)."""
import json, sys, time
TOOLS=[{"name":"echo","description":"Echo text back","inputSchema":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}},
       {"name":"add","description":"Add two numbers","inputSchema":{"type":"object","properties":{"a":{"type":"number"},"b":{"type":"number"}},"required":["a","b"]}},
       {"name":"boom","description":"Always errors","inputSchema":{"type":"object","properties":{}}},
       {"name":"slow","description":"Sleeps 2s","inputSchema":{"type":"object","properties":{}}}]
def send(m): sys.stdout.write(json.dumps(m)+"\n"); sys.stdout.flush()
for line in sys.stdin:
    line=line.strip()
    if not line: continue
    msg=json.loads(line)
    mid=msg.get("id"); method=msg.get("method","")
    if method=="initialize":
        send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":"2025-03-26","capabilities":{"tools":{}},"serverInfo":{"name":"fake-mcp","version":"0.1"}}})
    elif method=="notifications/initialized":
        pass
    elif method=="tools/list":
        send({"jsonrpc":"2.0","id":mid,"result":{"tools":TOOLS}})
    elif method=="tools/call":
        name=msg["params"]["name"]; args=msg["params"].get("arguments",{})
        if name=="echo": send({"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text":"ECHO: "+args.get("text","")}]}})
        elif name=="add": send({"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text":str(args["a"]+args["b"])}]}})
        elif name=="boom": send({"jsonrpc":"2.0","id":mid,"result":{"isError":True,"content":[{"type":"text","text":"deliberate failure"}]}})
        elif name=="slow":
            time.sleep(2); send({"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text":"finally"}]}})
        else: send({"jsonrpc":"2.0","id":mid,"error":{"code":-32602,"message":"no such tool"}})
    elif mid is not None:
        send({"jsonrpc":"2.0","id":mid,"error":{"code":-32601,"message":"nope"}})
