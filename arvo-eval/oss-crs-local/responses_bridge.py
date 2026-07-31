#!/usr/bin/env python3
"""Bridge proxy: translate OpenAI /v1/responses -> /v1/chat/completions for a
llama.cpp box whose /v1/responses grammar handling is broken. Everything else is
forwarded transparently to the box at 127.0.0.1:8080.

We call the box non-streaming and, if the client asked for stream=true, synthesize
a minimal-but-complete Responses SSE event sequence from the full result.
"""
import http.server, socketserver, urllib.request, urllib.error, json, time, uuid, datetime, sys
import os, tempfile

BOX = os.environ.get("BRIDGE_BOX", "http://127.0.0.1:8080")
LOGF = os.environ.get("BRIDGE_LOG", os.path.join(tempfile.gettempdir(), "responses_bridge.log"))

def log(m):
    with open(LOGF, "a") as f:
        f.write(f"{datetime.datetime.now().isoformat()} {m}\n")

def _uid(p): return f"{p}_{uuid.uuid4().hex[:24]}"

# JSON-schema validation keywords that this llama.cpp build's GBNF grammar builder
# chokes on (esp. a large maxLength on Workflow.script -> "failed to parse grammar").
# They are pure validation constraints; stripping them keeps the model's view intact
# (descriptions/types remain) while making the toolset grammar-safe.
_STRIP_KEYS = {"maxLength","minLength","pattern","format","minimum","maximum",
               "exclusiveMinimum","exclusiveMaximum","multipleOf","minItems","maxItems",
               "uniqueItems","minProperties","maxProperties"}
def sanitize_schema(o):
    if isinstance(o, dict):
        return {k: sanitize_schema(v) for k, v in o.items() if k not in _STRIP_KEYS}
    if isinstance(o, list):
        return [sanitize_schema(x) for x in o]
    return o

# ---------- request translation: responses -> chat ----------
def responses_to_chat(rb):
    msgs = []
    instr = rb.get("instructions")
    if instr:
        msgs.append({"role": "system", "content": instr})
    pend = None  # pending assistant message accumulating text + tool_calls
    def flush():
        nonlocal pend
        if pend is not None:
            if not pend.get("content") and not pend.get("tool_calls"):
                pend["content"] = ""
            msgs.append(pend); pend = None
    def text_of(content):
        if isinstance(content, str): return content
        out = []
        for c in content or []:
            if isinstance(c, dict) and c.get("type") in ("input_text","output_text","text","refusal"):
                out.append(c.get("text") or c.get("refusal") or "")
            elif isinstance(c, str): out.append(c)
        return "".join(out)
    inp = rb.get("input")
    if isinstance(inp, str):
        msgs.append({"role":"user","content":inp})
        inp = []
    for it in inp or []:
        t = it.get("type", "message")
        if t == "message":
            role = it.get("role","user")
            txt = text_of(it.get("content"))
            if role == "assistant":
                if pend is None: pend = {"role":"assistant","content":txt}
                else: pend["content"] = (pend.get("content") or "") + txt
            else:
                flush(); msgs.append({"role":role,"content":txt})
        elif t == "function_call":
            if pend is None: pend = {"role":"assistant","content":None}
            pend.setdefault("tool_calls", []).append({
                "id": it.get("call_id") or it.get("id") or _uid("call"),
                "type":"function",
                "function":{"name":it.get("name",""),"arguments":it.get("arguments","") or ""}})
        elif t == "function_call_output":
            flush()
            msgs.append({"role":"tool","tool_call_id":it.get("call_id") or it.get("id") or "",
                         "content": it.get("output","") if isinstance(it.get("output"),str) else json.dumps(it.get("output"))})
        else:
            # unknown item -> best effort as user text
            flush(); msgs.append({"role":"user","content":text_of(it.get("content"))})
    flush()
    # Mistral/Devstral template demands strict user/assistant alternation (tool msgs
    # allowed). Merge consecutive same-role user/assistant messages so quirks in the
    # input (e.g. system-reminders as separate user items) can't break the template.
    norm = []
    for m in msgs:
        if norm and m["role"] == norm[-1]["role"] and m["role"] in ("user","assistant"):
            prev = norm[-1]
            pc, mc = prev.get("content") or "", m.get("content") or ""
            merged = (pc + ("\n" if pc and mc else "") + mc) if (pc or mc) else None
            prev["content"] = merged
            if m.get("tool_calls"): prev.setdefault("tool_calls", []).extend(m["tool_calls"])
        else:
            norm.append(dict(m))
    msgs = norm
    chat = {"model": rb.get("model","local"), "messages": msgs, "stream": False}
    if rb.get("max_output_tokens"): chat["max_tokens"] = rb["max_output_tokens"]
    # Pass through the client's sampling. (A Qwen3-Coder-Next-specific override --
    # temp 0.7 / top_k 20 / DRY -- lived here Jul 29-30 to fight that model's verbatim
    # repetition loops; removed on the revert to Devstral, which drives the loop fine at
    # its own temperature.)
    if rb.get("temperature") is not None: chat["temperature"] = rb["temperature"]
    if rb.get("top_p") is not None: chat["top_p"] = rb["top_p"]
    # tools: responses {type:function,name,description,parameters} -> chat {type:function,function:{...}}
    tools = rb.get("tools")
    if tools:
        ct = []
        for tl in tools:
            if tl.get("type") == "function":
                if "function" in tl: ct.append(tl)
                else: ct.append({"type":"function","function":{
                    "name":tl.get("name"),"description":tl.get("description",""),
                    "parameters":tl.get("parameters",{"type":"object","properties":{}})}})
        # strip grammar-hostile validation keywords from every tool's parameters
        for c in ct:
            fn = c.get("function", {})
            if "parameters" in fn:
                fn["parameters"] = sanitize_schema(fn["parameters"])
        if ct: chat["tools"] = ct
    tc = rb.get("tool_choice")
    if tc is not None:
        if isinstance(tc, dict) and tc.get("type") == "function" and "function" not in tc:
            chat["tool_choice"] = {"type":"function","function":{"name":tc.get("name")}}
        else:
            chat["tool_choice"] = tc
    return chat

# ---------- response translation: chat -> responses ----------
def chat_to_responses(cj, model):
    choice = (cj.get("choices") or [{}])[0]
    msg = choice.get("message", {}) or {}
    output = []
    text = msg.get("content") or ""
    if text:
        output.append({"type":"message","id":_uid("msg"),"status":"completed","role":"assistant",
                       "content":[{"type":"output_text","text":text,"annotations":[]}]})
    for tcx in (msg.get("tool_calls") or []):
        fn = tcx.get("function",{})
        output.append({"type":"function_call","id":_uid("fc"),
                       "call_id": tcx.get("id") or _uid("call"),
                       "name": fn.get("name",""), "arguments": fn.get("arguments","") or "",
                       "status":"completed"})
    u = cj.get("usage",{}) or {}
    resp = {
        "id": _uid("resp"), "object":"response", "created_at": int(time.time()),
        "status":"completed", "model": model, "output": output,
        "output_text": text,
        "usage": {"input_tokens": u.get("prompt_tokens",0), "output_tokens": u.get("completion_tokens",0),
                  "total_tokens": u.get("total_tokens",0)},
        "parallel_tool_calls": True, "tool_choice":"auto", "tools":[], "metadata":{},
        "error": None, "incomplete_details": None, "instructions": None, "temperature": None, "top_p": None,
    }
    return resp

def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()

def stream_response(resp):
    """Yield a complete Responses SSE sequence for a finished response object."""
    seq = 0
    def ev(name, extra):
        nonlocal seq
        d = {"type": name, "sequence_number": seq}; seq += 1; d.update(extra); return sse(name, d)
    base = {k: resp[k] for k in resp}
    inprog = dict(base); inprog["status"] = "in_progress"; inprog["output"] = []
    yield ev("response.created", {"response": inprog})
    yield ev("response.in_progress", {"response": inprog})
    for idx, item in enumerate(resp["output"]):
        yield ev("response.output_item.added", {"output_index": idx, "item": {**item, "status":"in_progress"}})
        if item["type"] == "message":
            txt = item["content"][0]["text"]
            part = {"type":"output_text","text":"","annotations":[]}
            yield ev("response.content_part.added", {"item_id":item["id"],"output_index":idx,"content_index":0,"part":part})
            yield ev("response.output_text.delta", {"item_id":item["id"],"output_index":idx,"content_index":0,"delta":txt})
            yield ev("response.output_text.done", {"item_id":item["id"],"output_index":idx,"content_index":0,"text":txt})
            yield ev("response.content_part.done", {"item_id":item["id"],"output_index":idx,"content_index":0,"part":{"type":"output_text","text":txt,"annotations":[]}})
        elif item["type"] == "function_call":
            yield ev("response.function_call_arguments.delta", {"item_id":item["id"],"output_index":idx,"delta":item["arguments"]})
            yield ev("response.function_call_arguments.done", {"item_id":item["id"],"output_index":idx,"arguments":item["arguments"]})
        yield ev("response.output_item.done", {"output_index": idx, "item": item})
    yield ev("response.completed", {"response": resp})

# ---------- http ----------
import threading
_BOX_LOCK = threading.Lock()  # box is single-slot (-np 1): serialize so concurrent
                              # agent calls (main + background) can't contend/deadlock
def call_box(path, body_bytes, headers, method="POST"):
    req = urllib.request.Request(BOX + path, data=body_bytes if body_bytes else None, method=method)
    for k, v in headers.items():
        if k.lower() not in ("host","content-length","accept-encoding"): req.add_header(k, v)
    with _BOX_LOCK:
        try:
            r = urllib.request.urlopen(req, timeout=600); return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:
            return 502, json.dumps({"error":{"message":str(e)}}).encode()

class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 300  # per-connection socket timeout so a stuck read never hangs a thread forever
    def _send(self, status, body, ctype="application/json"):
        # Force connection close on every response: avoids the keep-alive/streaming
        # ambiguity that deadlocked LiteLLM<->bridge (client waiting for more, bridge
        # waiting for the next request). New conn per request is cheap on this hop.
        self.close_connection = True
        try:
            self.send_response(status); self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close"); self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            log(f"send err: {e}")
    def do_GET(self):
        st, b = call_box(self.path, b"", self.headers, "GET"); self._send(st, b)
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        if not self.path.rstrip("/").endswith("/responses"):
            st, b = call_box(self.path, raw, self.headers, "POST"); self._send(st, b); return
        # translate responses -> chat
        try:
            rb = json.loads(raw or b"{}")
        except Exception as e:
            self._send(400, json.dumps({"error":{"message":f"bad json: {e}"}}).encode()); return
        want_stream = bool(rb.get("stream"))
        chat = responses_to_chat(rb)
        st, cb = call_box("/v1/chat/completions", json.dumps(chat).encode(),
                          {"Content-Type":"application/json"}, "POST")
        if st != 200:
            log(f"BOX chat/completions -> {st}: {cb[:300].decode('utf-8','replace')}")
            # dump the failing tools for offline bisection
            try:
                import os
                dumpf = "/tmp/claude-1000/-home-ld-Projects-Panacea/b6baab47-729f-41b8-8acc-92c2e9a7bb14/scratchpad/failing_tools.json"
                if not os.path.exists(dumpf) and chat.get("tools"):
                    with open(dumpf, "w") as df: json.dump(chat["tools"], df)
                    log(f"dumped {len(chat['tools'])} failing tools -> failing_tools.json")
            except Exception as _e:
                log(f"dump err {_e}")
            self._send(st, cb); return
        try:
            cj = json.loads(cb)
            resp = chat_to_responses(cj, rb.get("model","local"))
        except Exception as e:
            log(f"translate-resp error: {e}; body={cb[:300]!r}")
            self._send(502, json.dumps({"error":{"message":f"bridge translate: {e}"}}).encode()); return
        ntools = len(cj.get("choices",[{}])[0].get("message",{}).get("tool_calls") or [])
        log(f"OK responses<-chat stream={want_stream} out_items={len(resp['output'])} tool_calls={ntools}")
        if not want_stream:
            self._send(200, json.dumps(resp).encode()); return
        # stream synthesized SSE -- Connection: close so the client reads until EOF.
        # (the old keep-alive + no-Content-Length response was ambiguous: the client
        # couldn't tell the stream ended and held the socket open -> deadlock.)
        self.close_connection = True
        try:
            self.send_response(200); self.send_header("Content-Type","text/event-stream")
            self.send_header("Cache-Control","no-cache")
            self.send_header("Connection","close"); self.end_headers()
            for chunk in stream_response(resp):
                self.wfile.write(chunk); self.wfile.flush()
        except Exception as e:
            log(f"stream write aborted: {e}")
    def log_message(self, *a): pass

class Bridge(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
if __name__ == "__main__":
    with Bridge(("0.0.0.0", 8099), H) as httpd:
        log("responses-bridge up :8099 -> " + BOX)
        httpd.serve_forever()
