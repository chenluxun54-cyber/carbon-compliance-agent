"""
auto_fix.py — 闭环自修复协调器

运行闭环直到所有 Issue 解决：
  fix_agent → verify_agent → 失败的 issue 回到 'new' → monitor 重新分析 → 再 fix

5 轮后仍未解决的问题写入 MANUAL_REVIEW.md 供人工处理。
"""

import asyncio
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import fix_agent
import verify_agent

USAGE_LOG     = Path(__file__).parent / "USAGE_LOG.md"
MANUAL_REVIEW = Path(__file__).parent / "MANUAL_REVIEW.md"
MAX_CYCLES    = 5


def count_issues_by_status(status: str) -> int:
    if not USAGE_LOG.exists():
        return 0
    return len(re.findall(rf"^- status: {status}$",
                          USAGE_LOG.read_text(encoding="utf-8"), re.MULTILINE))


def get_issues_by_status(status: str) -> list[str]:
    """Return full ISSUE blocks matching the given status."""
    if not USAGE_LOG.exists():
        return []
    content = USAGE_LOG.read_text(encoding="utf-8")
    blocks = re.split(r"(?=^## ISSUE-)", content, flags=re.MULTILINE)
    result = []
    for block in blocks:
        if not block.startswith("## ISSUE-"):
            continue
        if re.search(rf"^- status: {status}$", block, re.MULTILINE):
            result.append(block.strip())
    return result


def escalate_to_manual_review() -> None:
    """Write unresolved issues to MANUAL_REVIEW.md for human inspection."""
    unresolved = get_issues_by_status("new") + get_issues_by_status("needs_review")
    if not unresolved:
        return

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = (
        f"# 🚨 Manual Review Required — {ts}\n\n"
        f"Auto-fix ran {MAX_CYCLES} cycles but could not resolve the following "
        f"{len(unresolved)} issue(s). Please review and fix manually.\n\n"
        "---\n\n"
    )

    existing = MANUAL_REVIEW.read_text(encoding="utf-8") if MANUAL_REVIEW.exists() else ""
    with MANUAL_REVIEW.open("w", encoding="utf-8") as f:
        f.write(header + "\n\n".join(unresolved) + "\n\n" + existing)

    print(f"\n{'='*60}")
    print(f"  🚨 MANUAL REVIEW REQUIRED — {len(unresolved)} issue(s)")
    print(f"{'='*60}")
    for block in unresolved:
        issue_id  = re.search(r"^## (ISSUE-\S+)", block, re.MULTILINE)
        severity  = re.search(r"^- severity: (.+)$", block, re.MULTILINE)
        error     = re.search(r"^- error: (.+)$", block, re.MULTILINE)
        fix_hint  = re.search(r"^- fix_hint: (.+)$", block, re.MULTILINE)
        print(f"\n  [{severity.group(1).upper() if severity else '?'}] "
              f"{issue_id.group(1) if issue_id else '?'}")
        if error:
            print(f"    Error:    {error.group(1)[:80]}")
        if fix_hint:
            print(f"    Hint:     {fix_hint.group(1)[:80]}")
    print(f"\n  → Full details saved to: MANUAL_REVIEW.md")


async def main():
    if not os.environ.get("MINIMAX_API_KEY"):
        print("[auto_fix] ERROR: MINIMAX_API_KEY not set.")
        sys.exit(1)

    print("=" * 60)
    print("  Carbon Agent Auto-Fix Loop")
    print("=" * 60)

    # Ensure server is running before first verify cycle
    fix_agent.restart_server()
    await fix_agent.wait_for_server()

    for cycle in range(1, MAX_CYCLES + 1):
        new_count = count_issues_by_status("new")
        print(f"\n━━━ Cycle {cycle}/{MAX_CYCLES} — {new_count} new issue(s) ━━━")

        if new_count == 0:
            print("\n✅ All issues resolved!")
            break

        fixes = await fix_agent.run()

        if fixes == 0:
            remaining = count_issues_by_status("new")
            if remaining == 0:
                print("\n✅ All issues resolved (or marked needs_review)!")
            else:
                print(f"\n⚠ {remaining} issue(s) could not be auto-fixed.")
            break

        passed, failed = await verify_agent.run()
        print(f"\n[auto_fix] Cycle {cycle}: {passed} fixed ✅  {failed} failed ❌")

        if failed == 0 and count_issues_by_status("new") == 0:
            print("\n✅ All issues fixed and verified!")
            break

        if failed > 0:
            print("[auto_fix] Waiting 10s for monitor to re-analyze...")
            await asyncio.sleep(10)

    else:
        # Exhausted all cycles — escalate to human
        escalate_to_manual_review()

    # Final status summary
    print("\n" + "=" * 60)
    print("  Final Status")
    print("=" * 60)
    for status in ("fixed", "new", "needs_review", "verifying"):
        count = count_issues_by_status(status)
        if count:
            emoji = {"fixed": "✅", "new": "❌", "needs_review": "🚨", "verifying": "🔄"}
            print(f"  {emoji.get(status, '?')} {status}: {count}")

    # Always escalate leftover needs_review issues
    nr = count_issues_by_status("needs_review")
    new = count_issues_by_status("new")
    if nr + new > 0:
        escalate_to_manual_review()


if __name__ == "__main__":
    asyncio.run(main())
