"""
verify_agent.py — 验证 Agent

读取 USAGE_LOG.md 中 status:verifying 的 Issue → 向 localhost:8000 发 trigger 消息
→ 检查是否还有错误 → 通过则 status:fixed，失败则 status:new（供 monitor 重新分析）
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import httpx

BASE_URL = "http://localhost:8000"


async def wait_for_server(timeout: int = 40) -> bool:
    """Poll /provider until server responds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(f"{BASE_URL}/provider", timeout=2)
                if r.status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False

BASE = Path(__file__).parent
USAGE_LOG = BASE / "USAGE_LOG.md"


def parse_issues(content: str) -> list[dict]:
    """Extract all ISSUE blocks from USAGE_LOG.md."""
    blocks = re.split(r"(?=^## ISSUE-)", content, flags=re.MULTILINE)
    issues = []
    for block in blocks:
        if not block.startswith("## ISSUE-"):
            continue
        issue = {"raw": block.strip()}
        for field in ("severity", "category", "status", "file", "trigger"):
            m = re.search(rf"^- {field}: (.+)$", block, re.MULTILINE)
            issue[field] = m.group(1).strip().strip('"') if m else ""
        m = re.match(r"## (ISSUE-\S+)", block)
        issue["id"] = m.group(1) if m else ""
        issues.append(issue)
    return issues


def update_issue_status(issue_id: str, new_status: str, new_error: str = "") -> None:
    """Update status and optionally append new error evidence."""
    content = USAGE_LOG.read_text(encoding="utf-8")
    pattern = rf"(## {re.escape(issue_id)}.*?^- status: )\w+"
    new_content = re.sub(pattern, rf"\g<1>{new_status}", content,
                         flags=re.DOTALL | re.MULTILINE)
    if new_error:
        # Append re-verification failure as additional evidence
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        note = f"\n- re_verify_failed_{ts}: {new_error}\n"
        new_content = new_content.replace(
            f"## {issue_id}",
            f"## {issue_id}{note}",
            1,
        )
    USAGE_LOG.write_text(new_content, encoding="utf-8")


async def verify(trigger: str, category: str) -> tuple[bool, str]:
    """
    Send trigger message to server and check for errors.
    Returns (passed, error_description).
    """
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            # Create a fresh session
            resp = await c.post(f"{BASE_URL}/new_session", json={})
            sid = resp.json()["session_id"]

            t0 = time.monotonic()
            error_msg = ""

            async with c.stream(
                "POST", f"{BASE_URL}/chat",
                json={"session_id": sid, "message": trigger},
            ) as r:
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "error":
                        error_msg = event.get("content", "unknown error")
                        break

            elapsed = time.monotonic() - t0

            if error_msg:
                return False, f"Error response: {error_msg}"
            if category == "performance" and elapsed > 10:
                return False, f"Still slow: {elapsed:.1f}s (threshold 10s)"
            return True, ""

    except httpx.ConnectError:
        return False, "Server not reachable at localhost:8000"
    except Exception as exc:
        return False, f"Verification exception: {exc}"


async def run() -> tuple[int, int]:
    """
    Run verify agent.
    Returns (passed_count, failed_count).
    """
    if not USAGE_LOG.exists():
        print("[verify_agent] USAGE_LOG.md not found.")
        return 0, 0

    content = USAGE_LOG.read_text(encoding="utf-8")
    issues = parse_issues(content)
    verifying = [i for i in issues if i["status"] == "verifying"]

    if not verifying:
        print("[verify_agent] No issues in 'verifying' state.")
        return 0, 0

    print("[verify_agent] Waiting for server to be ready...")
    if not await wait_for_server():
        print("[verify_agent] ⚠ Server not available — skipping verification")
        return 0, 0

    print(f"[verify_agent] Testing {len(verifying)} issue(s)...")
    passed = failed = 0

    for issue in verifying:
        issue_id = issue["id"]
        trigger = issue.get("trigger", "")
        category = issue.get("category", "")

        if not trigger:
            print(f"  [verify] {issue_id}: no trigger — marking fixed")
            update_issue_status(issue_id, "fixed")
            passed += 1
            continue

        print(f"  [verify] {issue_id} — sending: \"{trigger[:60]}\"")
        ok, err = await verify(trigger, category)

        if ok:
            print(f"  [verify] ✅ PASSED — marking fixed")
            update_issue_status(issue_id, "fixed")
            passed += 1
        else:
            print(f"  [verify] ❌ FAILED — {err}")
            update_issue_status(issue_id, "new", new_error=err)
            failed += 1

        await asyncio.sleep(2)  # brief pause between tests

    print(f"\n[verify_agent] Done. {passed} passed, {failed} failed.")
    return passed, failed


if __name__ == "__main__":
    asyncio.run(run())
