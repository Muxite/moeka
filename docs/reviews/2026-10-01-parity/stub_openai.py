"""Local OpenAI-compatible stub. Usage: stub_openai.py PORT OUTFILE. Records every request body and headers to OUTFILE."""
import json, sys
from aiohttp import web
log=[]
async def h(req):
    body=await req.json()
    log.append({"path":req.path,"headers":{k:v for k,v in req.headers.items() if k.lower() in("authorization","content-type","http-referer","x-title","user-agent")},"body":body})
    json.dump(log,open(sys.argv[2],"w"),indent=1)
    if body.get("stream"):
        resp=web.StreamResponse(headers={"Content-Type":"text/event-stream"}); await resp.prepare(req)
        for ch in [{"id":"1","object":"chat.completion.chunk","model":"m","choices":[{"index":0,"delta":{"role":"assistant","content":"STUB OK"},"finish_reason":None}]},
                   {"id":"1","object":"chat.completion.chunk","model":"m","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}]:
            await resp.write(("data: "+json.dumps(ch)+"\n\n").encode())
        await resp.write(b"data: [DONE]\n\n"); return resp
    return web.json_response({"id":"1","object":"chat.completion","model":"m","choices":[{"index":0,"message":{"role":"assistant","content":"STUB OK"},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}})
app=web.Application(); app.router.add_route("*","/{tail:.*}",h)
web.run_app(app,host="127.0.0.1",port=int(sys.argv[1]),print=None)
