"""
fix_agent.py — 自动修复 Agent

读取 USAGE_LOG.md 中 status:new 的 Issue → 调用 MiniMax API 生成 fix（行号替换）
→ 应用代码修改 → 更新 status 为 verifying → 重启 uvicorn server
"""

import asyncio
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

import anthropic

BASE = Path(__file__).parent
USAGE_LOG = BASE / "USAGE_LOG.md"

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# Categories that cannot be reliably auto-fixed with code changes
SKIP_CATEGORIES = {"response_quality", "missing_feature", "performance", "config"}

_PROVIDER = os.environ.get("MODEL_PROVIDER", "").lower()
_MINIMAX_KEY = os.environ.get("MINIMAX_API_KEY", "")
_ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Auto-detect: prefer explicitly set MODEL_PROVIDER; fall back to whichever key is present
if _PROVIDER == "anthropic" or (not _MINIMAX_KEY and _ANTHROPIC_KEY):
    client = anthropic.AsyncAnthropic(api_key=_ANTHROPIC_KEY)
    MODEL = "claude-haiku-4-5-20251001"
else:
    client = anthropic.AsyncAnthropic(
        base_url="https://api.minimaxi.com/anthropic",
        api_key=_MINIMAX_KEY,
    )
    MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-Text-01")

FIX_PROMPT = """\
You are a code fixer for a FastAPI carbon-compliance-agent (Python).
The agent uses Anthropic/MiniMax SDK, has tools: carbon_score, search_policies, get_policy_detail, record_data, start_product_calc.

ISSUE TO FIX:
{issue_block}

FILE: {filename}
CONTENT (with line numbers):
{numbered_content}

Analyze the issue and output a minimal targeted fix using line numbers.
Output EXACTLY this format and nothing else:

FIX_DESCRIPTION: <one-line summary of the fix>
FILE: {filename}
START_LINE: <first line to replace, 1-indexed>
END_LINE: <last line to replace, 1-indexed, inclusive>
REPLACEMENT:
```python
<replacement code that replaces lines START_LINE through END_LINE entirely>
```

Rules:
- Fix ONLY what the issue describes — no refactoring
- START_LINE and END_LINE must be valid line numbers shown above
- Replacement can be longer or shorter than the original range
- If this is an environment/config/credential issue that cannot be fixed in code, output exactly:
  CANNOT_FIX: <reason>
"""


def parse_issues(content: str) -> list:
    blocks = re.split(r"(?=^## ISSUE-)", content, flags=re.MULTILINE)
    issues = []
    for block in blocks:
        if not block.startswith("## ISSUE-"):
            continue
        issue = {"raw": block.strip()}
        for field in ("severity", "category", "status", "file", "line", "trigger"):
            m = re.search(rf"^- {field}: (.+)$", block, re.MULTILINE)
            issue[field] = m.group(1).strip() if m else ""
        m = re.match(r"## (ISSUE-\S+)", block)
        issue["id"] = m.group(1) if m else ""
        issues.append(issue)
    return issues


def update_issue_status(issue_id: str, new_status: str) -> None:
    content = USAGE_LOG.read_text(encoding="utf-8")
    pattern = rf"(## {re.escape(issue_id)}.*?^- status: )\w+"
    new_content = re.sub(pattern, rf"\g<1>{new_status}", content,
                         flags=re.DOTALL | re.MULTILINE)
    USAGE_LOG.write_text(new_content, encoding="utf-8")


def numbered(text: str) -> str:
    """Add line numbers to file content for the prompt."""
    lines = text.splitlines()
    width = len(str(len(lines)))
    return "\n".join(f"{str(i+1).rjust(width)}: {l}" for i, l in enumerate(lines))


def parse_fix_response(response: str) -> Optional[dict]:
    text = response.strip()

    if text.startswith("CANNOT_FIX:"):
        return {"cannot_fix": True, "reason": text.replace("CANNOT_FIX:", "").strip()}

    desc_m  = re.search(r"FIX_DESCRIPTION: (.+)", text)
    file_m  = re.search(r"^FILE: (.+)$", text, re.MULTILINE)
    start_m = re.search(r"START_LINE: (\d+)", text)
    end_m   = re.search(r"END_LINE: (\d+)", text)
    repl_m  = re.search(r"REPLACEMENT:\n```python\n(.*?)```", text, re.DOTALL)

    if not (start_m and end_m and repl_m):
        return None

    return {
        "cannot_fix":  False,
        "description": desc_m.group(1).strip() if desc_m else "",
        "file":        file_m.group(1).strip() if file_m else "",
        "start_line":  int(start_m.group(1)),
        "end_line":    int(end_m.group(1)),
        "replacement": repl_m.group(1),
    }


def apply_fix(fix: dict) -> bool:
    import ast as _ast
    target = BASE / fix["file"]
    if not target.exists():
        print(f"  [fix] File not found: {fix['file']}")
        return False

    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    start = fix["start_line"] - 1   # 0-indexed
    end   = fix["end_line"]         # slice end is exclusive

    if start < 0 or end > len(lines) or start > end:
        print(f"  [fix] Invalid line range {fix['start_line']}–{fix['end_line']} "
              f"(file has {len(lines)} lines)")
        return False

    replacement = fix["replacement"]
    if replacement and not replacement.endswith("\n"):
        replacement += "\n"
    replacement_lines = replacement.splitlines(keepends=True)

    new_content = "".join(lines[:start] + replacement_lines + lines[end:])

    # Syntax-check .py files before writing — reject broken fixes
    if target.suffix == ".py":
        try:
            _ast.parse(new_content)
        except SyntaxError as e:
            print(f"  [fix] ❌ Syntax error in generated fix — REVERTED: {e}")
            return False

    target.write_text(new_content, encoding="utf-8")
    print(f"  [fix] ✅ Applied to {fix['file']} lines {fix['start_line']}–{fix['end_line']}: "
          f"{fix['description']}")
    return True


async def generate_fix(issue: dict, retries: int = 2) -> Optional[dict]:
    filename = issue.get("file", "unknown")
    target = BASE / filename
    if not target.exists():
        print(f"  [fix] Source file not found: {filename}")
        return {"cannot_fix": True, "reason": f"file {filename} not found"}

    file_content = target.read_text(encoding="utf-8")
    prompt = FIX_PROMPT.format(
        issue_block=issue["raw"],
        filename=filename,
        numbered_content=numbered(file_content[:8000]),
    )

    for attempt in range(1, retries + 1):
        try:
            resp = await client.messages.create(
                model=MODEL,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            result = parse_fix_response(resp.content[0].text)
            if result is None:
                print(f"  [fix] Could not parse response (attempt {attempt})")
                continue
            return result
        except Exception as exc:
            print(f"  [fix] API error (attempt {attempt}): {exc}")
            await asyncio.sleep(2)

    return None


def restart_server() -> None:
    minimax_key = os.environ.get("MINIMAX_API_KEY", "")
    print("\n[fix_agent] Restarting uvicorn server...")
    subprocess.run(["pkill", "-f", "uvicorn"], capture_output=True)
    time.sleep(2)
    env = os.environ.copy()
    env["MODEL_PROVIDER"] = "minimax"
    env["MINIMAX_API_KEY"] = minimax_key
    subprocess.Popen(
        ["python3", "-m", "uvicorn", "agent:app", "--port", "8000"],
        cwd=BASE, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print("[fix_agent] Server starting... waiting 6s")
    time.sleep(6)


async def wait_for_server(timeout: int = 30) -> bool:
    import httpx
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get("http://localhost:8000/provider", timeout=2)
                if r.status_code == 200:
                    print("[fix_agent] Server ready ✅")
                    return True
        except Exception:
            pass
        await asyncio.sleep(1)
    print("[fix_agent] ⚠ Server did not start in time")
    return False


async def run() -> int:
    """Run fix agent. Returns number of fixes applied."""
    if not USAGE_LOG.exists():
        print("[fix_agent] USAGE_LOG.md not found.")
        return 0

    content = USAGE_LOG.read_text(encoding="utf-8")
    issues = parse_issues(content)
    new_issues = [i for i in issues if i["status"] == "new"]

    if not new_issues:
        print("[fix_agent] No new issues to fix.")
        return 0

    new_issues.sort(key=lambda i: SEVERITY_ORDER.get(i["severity"], 99))
    print(f"[fix_agent] {len(new_issues)} new issue(s) to fix.")

    fixes_applied = 0
    for issue in new_issues:
        issue_id = issue["id"]
        category = issue.get("category", "")
        print(f"\n[fix_agent] {issue_id} ({issue['severity']}, {category})")

        if category in SKIP_CATEGORIES:
            update_issue_status(issue_id, "needs_review")
            print(f"  [fix] → needs_review (subjective quality — manual fix)")
            continue

        fix = await generate_fix(issue)
        if fix is None:
            print(f"  [fix] → skipped (could not generate fix after retries)")
            continue
        if fix.get("cannot_fix"):
            print(f"  [fix] → CANNOT_FIX: {fix.get('reason')}")
            update_issue_status(issue_id, "needs_review")
            continue

        if apply_fix(fix):
            update_issue_status(issue_id, "verifying")
            fixes_applied += 1
        else:
            print(f"  [fix] → failed to apply")

    if fixes_applied > 0:
        restart_server()
        await wait_for_server()
    else:
        print("\n[fix_agent] No fixes applied — skipping server restart.")

    print(f"\n[fix_agent] Done. {fixes_applied} fix(es) applied.")
    return fixes_applied


if __name__ == "__main__":
    asyncio.run(run())
