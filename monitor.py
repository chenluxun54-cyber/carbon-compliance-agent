"""
monitor.py — 本地实时监控 Agent

监听 testing_log.md 变化 → 调用 MiniMax API 分析 → 写入 USAGE_LOG.md 和 AGENT_JOURNAL.md
启动: python3 monitor.py
"""

import asyncio
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import anthropic

# Force line-buffered output so logs appear immediately even when piped
sys.stdout.reconfigure(line_buffering=True)

BASE = Path(__file__).parent
LOG_FILE  = BASE / "testing_log.md"
USAGE_LOG = BASE / "USAGE_LOG.md"
JOURNAL   = BASE / "AGENT_JOURNAL.md"

client = anthropic.AsyncAnthropic(
    base_url="https://api.minimaxi.com/anthropic",
    api_key=os.environ.get("MINIMAX_API_KEY", ""),
)
MODEL = "MiniMax-Text-01"

ANALYZE_PROMPT = """\
You are a QA analyst for a carbon-compliance-agent (FastAPI + streaming + Anthropic tools).
The agent helps users: query company carbon scores, search policies, calculate product carbon footprint.

Each SESSION block below contains: user message, tool calls (✅/❌ with timing), AI response, and errors.
Analyze ALL sessions and identify EVERY problem — not just hard errors. This includes:
- Python exceptions or API errors
- Tool call failures (❌) or wrong results
- Slow responses (>10s total_elapsed)
- AI responses that seem wrong, incomplete, or unhelpful given the user's question
- Missing functionality (user expected something the agent couldn't do)
- Logic errors in reasoning
- Data quality issues
- Any other observable problem

For each problem output EXACTLY this format (an automated fix-agent will parse it):

## ISSUE-{date}-NNN
- severity: critical|high|medium|low
- category: api_error|logic_bug|data_bug|performance|response_quality|missing_feature|config|ui
- status: new
- file: {filename from traceback or best guess, e.g. scorer.py, agent.py}
- line: {line number if in traceback, else unknown}
- error: {exact error message OR clear one-line description}
- trigger: {exact user_message from the SESSION block}
- steps_to_reproduce: |
  1. Send: "{trigger}"
  2. {what happened}
- evidence: |
  {key snippet from the session that proves this is a problem}
- fix_hint: {concrete actionable suggestion, e.g. "add .get('key', 0) in scorer.py:45"}
- related_files: {other files likely involved}

If a SESSION has no problems, skip it silently.
After all issues end with exactly:
SUMMARY: N issues — {brief one-line description of main problems}
"""


def get_last_processed_ts() -> str:
    if not USAGE_LOG.exists():
        return "1970-01-01T00:00:00"
    matches = re.findall(r"processed_up_to: (.+)", USAGE_LOG.read_text(encoding="utf-8"))
    return matches[-1].strip() if matches else "1970-01-01T00:00:00"


def get_new_sessions(last_ts: str) -> str:
    if not LOG_FILE.exists():
        return ""
    content = LOG_FILE.read_text(encoding="utf-8")
    blocks = re.split(r"(?=## SESSION )", content)
    new_blocks = []
    for b in blocks:
        if not b.strip():
            continue
        header = b.split("\n")[0]  # "## SESSION 2026-06-22T15:30:00.123456"
        ts = header.replace("## SESSION ", "").strip()
        if ts > last_ts:
            new_blocks.append(b)
    return "\n".join(new_blocks)


def count_existing_issues() -> int:
    if not USAGE_LOG.exists():
        return 0
    return len(re.findall(r"^## ISSUE-", USAGE_LOG.read_text(encoding="utf-8"), re.MULTILINE))


async def analyze(sessions_text: str, start_issue_num: int) -> str:
    date_str = datetime.now().strftime("%Y%m%d")
    full_prompt = (
        ANALYZE_PROMPT.replace("{date}", date_str)
        + f"\n\nStart issue numbering from {start_issue_num:03d}.\n\n"
        + "SESSIONS TO ANALYZE:\n"
        + sessions_text
    )
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=3000,
        messages=[{"role": "user", "content": full_prompt}],
    )
    return resp.content[0].text


def init_files() -> None:
    if not USAGE_LOG.exists():
        USAGE_LOG.write_text(
            "# Carbon Agent — Issue Log\n"
            "<!-- fix-agent: read ISSUE blocks with status:new, fix, set status:fixed -->\n\n",
            encoding="utf-8",
        )
        print("[monitor] Created USAGE_LOG.md")
    if not JOURNAL.exists():
        JOURNAL.write_text(
            "# Carbon Compliance Agent — Journal\n\n"
            "## 🏗️ Build History\n"
            "_Updated periodically from git history and CLAUDE.md._\n\n",
            encoding="utf-8",
        )
        print("[monitor] Created AGENT_JOURNAL.md")


def write_reports(analysis: str) -> None:
    ts = datetime.now().isoformat()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    with USAGE_LOG.open("a", encoding="utf-8") as f:
        f.write(f"\n{analysis}\nprocessed_up_to: {ts}\n")

    summary_match = re.search(r"SUMMARY: (.+)", analysis)
    summary = summary_match.group(1).strip() if summary_match else "see USAGE_LOG.md"
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(f"\n## 📊 Scan — {now_str}\n{summary}\n")

    print(f"[monitor] {now_str} ✓ {summary}")

    # Trigger auto_fix.py if there are new issues and it's not already running
    new_issues = len(re.findall(r"^- status: new$", analysis, re.MULTILINE))
    if new_issues > 0:
        already_running = subprocess.run(
            ["pgrep", "-f", "auto_fix.py"], capture_output=True
        ).returncode == 0
        if already_running:
            print(f"[monitor] auto_fix.py already running — skipping trigger")
        else:
            print(f"[monitor] {new_issues} new issue(s) → triggering auto_fix.py")
            subprocess.Popen(
                ["python3", "-u", str(BASE / "auto_fix.py")],
                env=os.environ.copy(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


async def main() -> None:
    if not os.environ.get("MINIMAX_API_KEY"):
        print("[monitor] ERROR: MINIMAX_API_KEY not set. Export it before running.")
        return

    init_files()
    print(f"[monitor] Watching {LOG_FILE} for new sessions...")
    last_mtime: float = 0.0  # always process existing content on first start

    while True:
        await asyncio.sleep(2)

        if not LOG_FILE.exists():
            continue

        mtime = LOG_FILE.stat().st_mtime
        if mtime <= last_mtime:
            continue

        last_mtime = mtime
        last_ts = get_last_processed_ts()
        sessions_text = get_new_sessions(last_ts)

        if not sessions_text.strip():
            continue

        session_count = sessions_text.count("## SESSION ")
        print(f"[monitor] {session_count} new session(s) detected — calling MiniMax...")

        try:
            issue_num = count_existing_issues() + 1
            analysis = await analyze(sessions_text, issue_num)
            write_reports(analysis)
        except Exception as exc:
            print(f"[monitor] Analysis failed: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
