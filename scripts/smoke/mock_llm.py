"""Minimal OpenAI-compatible mock: /v1/chat/completions (stream and non-stream) and /v1/models."""
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="application/json"):
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        self._send(200, json.dumps({"object":"list","data":[{"id":"mock-model","object":"model"}]}).encode())
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); req = json.loads(self.rfile.read(n) or b"{}")
        text = "MOCK-REPLY: smoke ok"
        usage = {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}
        base = {"id":"cmpl-mock","object":"chat.completion","created":int(time.time()),"model":req.get("model","mock-model")}
        if req.get("stream"):
            self.send_response(200); self.send_header("Content-Type","text/event-stream"); self.end_headers()
            def ev(d): self.wfile.write(b"data: "+json.dumps(d).encode()+b"\n\n"); self.wfile.flush()
            c = dict(base, object="chat.completion.chunk")
            ev(dict(c, choices=[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":None}]))
            ev(dict(c, choices=[{"index":0,"delta":{"content":text},"finish_reason":None}]))
            ev(dict(c, choices=[{"index":0,"delta":{},"finish_reason":"stop"}], usage=usage))
            self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush(); return
        self._send(200, json.dumps(dict(base, choices=[{"index":0,"message":{"role":"assistant","content":text},"finish_reason":"stop"}], usage=usage)).encode())

ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
