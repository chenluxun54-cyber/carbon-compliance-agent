"""
trace_endpoint.py
------------------
轻量级前端行为追踪端点。

作用:接收前端 trace-capture.js 上报的事件(点击 / console报错 /
未捕获异常 / 请求失败),逐行追加写入本地 trace.jsonl。

这一层只做"机械记录",不做任何判断或总结——把原始信号和你测试时
说的话放在一起,交给后面的 detector agent 去理解、转写成 rubric
格式的 bug 条目。

集成方式(在你现有的 FastAPI app 文件里,比如 main.py):

    from trace_endpoint import trace_router
    app.include_router(trace_router)

确认 trace-capture.js 能被当作静态文件访问到(跟 index.html 同目录,
或者你现在挂载静态文件的那个目录下即可),然后在 index.html 里加一行:

    <script src="/trace-capture.js"></script>
"""

from fastapi import APIRouter, Request
from pathlib import Path
from datetime import datetime, timezone
import json
import asyncio

trace_router = APIRouter()

# 默认写在当前工作目录下,如果想固定路径,改成绝对路径即可
TRACE_FILE = Path("trace.jsonl")

# 防止并发请求(比如快速连续点击)写入时互相截断
_lock = asyncio.Lock()


@trace_router.post("/__trace")
async def receive_trace(request: Request):
    """接收一条事件,补上服务端时间戳后追加写入 trace.jsonl"""
    try:
        payload = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}

    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    line = json.dumps(event, ensure_ascii=False) + "\n"

    async with _lock:
        with TRACE_FILE.open("a", encoding="utf-8") as f:
            f.write(line)

    return {"ok": True}


@trace_router.get("/__trace/recent")
async def recent_trace(n: int = 50):
    """方便你自己肉眼抽查最近 n 条记录,确认 trace 有没有正常写入"""
    if not TRACE_FILE.exists():
        return {"events": []}
    lines = TRACE_FILE.read_text(encoding="utf-8").strip().splitlines()
    parsed = []
    for l in lines[-n:]:
        try:
            parsed.append(json.loads(l))
        except json.JSONDecodeError:
            continue
    return {"events": parsed}


@trace_router.delete("/__trace")
async def clear_trace():
    """清空 trace.jsonl,方便每次新测试session前重置"""
    if TRACE_FILE.exists():
        TRACE_FILE.unlink()
    return {"ok": True}
