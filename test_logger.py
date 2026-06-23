"""
Testing logger — captures every /chat session to testing_log.md.
All events (tool calls, AI tokens, errors) are recorded immediately so
monitor.py can analyze them with MiniMax even if the server is shut down.
"""

import time
import traceback
from datetime import datetime
from pathlib import Path

LOG_FILE = Path(__file__).parent / "testing_log.md"


class TestingLogger:
    def __init__(self):
        self._trigger: str = ""
        self._session_start: float = 0.0
        self._tool_calls: list = []   # appended from executor threads — GIL-safe
        self._tokens: list = []
        self._errors: list = []

    def start_session(self, user_message: str) -> None:
        self._trigger = user_message
        self._session_start = time.monotonic()
        self._tool_calls = []
        self._tokens = []
        self._errors = []

    def log_tool_call(self, tool_name: str, args: dict,
                      result: str, elapsed: float,
                      success: bool, error: str = "") -> None:
        safe_args = {k: str(v)[:200] for k, v in args.items()}
        self._tool_calls.append({
            "tool": tool_name,
            "args": safe_args,
            "result": result[:500],
            "elapsed": round(elapsed, 2),
            "success": success,
            "error": error,
        })

    def accumulate_token(self, text: str) -> None:
        self._tokens.append(text)

    def log_exception(self, exc: Exception) -> None:
        self._errors.append({
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        })

    def end_session(self) -> None:
        total_elapsed = round(time.monotonic() - self._session_start, 2)
        ai_response = "".join(self._tokens)[:1000]

        tools_md = ""
        for t in self._tool_calls:
            status = "✅" if t["success"] else "❌"
            tools_md += f"  {status} {t['tool']}({t['args']}) [{t['elapsed']}s]\n"
            tools_md += f"     result: {t['result']}\n"
            if t["error"]:
                tools_md += f"     ERROR: {t['error']}\n"

        errors_md = ""
        for e in self._errors:
            errors_md += f"  {e['type']}: {e['message']}\n{e['traceback']}\n"

        ts = datetime.now().isoformat()
        entry = (
            f"\n## SESSION {ts}\n"
            f"user_message: {self._trigger}\n"
            f"total_elapsed: {total_elapsed}s\n"
            f"tool_calls:\n{tools_md if tools_md else '  (none)'}\n"
            f"ai_response: |\n  {ai_response if ai_response else '(empty or error)'}\n"
            f"errors: |\n  {errors_md if errors_md else 'none'}\n"
            f"---\n"
        )
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(entry)


# Global singleton — safe for single-user local testing
logger = TestingLogger()
