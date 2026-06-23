"""
run_tests.py — 碳足迹计算全链路测试
发送5个测试场景，打印每轮响应，最终汇总结果。
"""

import asyncio
import json
import httpx

BASE_URL = "http://localhost:8000"


TESTS = [
    {
        "name": "Test 1 — 一次性提供全部数据（铝制水杯）",
        "turns": [
            "我想计算一个铝制水杯的碳足迹。产品重量0.3千克，浙江工厂，生产每个杯子用0.8度电，主要材料是铝0.27千克加塑料0.03千克。"
        ],
    },
    {
        "name": "Test 2 — 多轮收集数据（PP塑料瓶）",
        "turns": [
            "帮我算一下PP塑料瓶的碳足迹",
            "每个瓶子重50克，广东工厂",
            "生产一个瓶子用0.15度电",
            "材料就是PP塑料0.05千克",
        ],
    },
    {
        "name": "Test 3 — 含可选字段（锂电池，含运输+包装+报废）",
        "turns": [
            "计算一个锂电池的碳足迹：重量2千克，江苏工厂，用电5度，材料锂电池2千克。运输800公里公路运输，外包装瓦楞纸箱0.1千克，产品报废后回收处理。"
        ],
    },
    {
        "name": "Test 4 — 未知材料降级处理（竹纤维杯）",
        "turns": [
            "帮我算一个竹纤维杯子的碳足迹，重量0.2千克，四川工厂，用电0.5度，材料是竹纤维0.2千克"
        ],
    },
    {
        "name": "Test 5 — 复用上下文（不锈钢杯，沿用浙江+0.8度电）",
        "turns": [
            "我想计算一个铝制水杯的碳足迹。产品重量0.3千克，浙江工厂，生产每个杯子用0.8度电，主要材料是铝0.27千克加塑料0.03千克。",
            "再算一个同款杯子但换成不锈钢材质，重量0.35千克，其他都和刚才一样",
        ],
    },
]


async def send_message(client: httpx.AsyncClient, session_id: str, message: str) -> str:
    """Stream one chat message and return the full AI text response."""
    full_text = ""
    tool_calls = []
    errors = []

    async with client.stream(
        "POST",
        f"{BASE_URL}/chat",
        json={"session_id": session_id, "message": message},
        timeout=120,
    ) as r:
        async for line in r.aiter_lines():
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue

            etype = event.get("type", "")
            if etype == "token":
                full_text += event.get("content", "")
            elif etype in ("status", "progress"):
                tool_calls.append(f"[{etype}] {event.get('content', '')}")
            elif etype == "tool_result":
                tool_calls.append(f"[tool_result] {str(event.get('content', ''))[:120]}")
            elif etype == "calc_complete":
                tool_calls.append("[calc_complete] ✅ 计算完成事件触发")
            elif etype == "error":
                errors.append(event.get("content", "unknown error"))

    return full_text.strip(), tool_calls, errors


async def run_test(test: dict) -> dict:
    name = test["name"]
    turns = test["turns"]
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")

    result = {"name": name, "passed": False, "error": None, "final_response": ""}
    all_tool_calls = []

    async with httpx.AsyncClient() as client:
        # Create a fresh session
        resp = await client.post(f"{BASE_URL}/new_session", json={})
        sid = resp.json()["session_id"]
        print(f"  session_id: {sid[:16]}...")

        for i, msg in enumerate(turns, 1):
            print(f"\n  [Turn {i}] User: {msg[:80]}{'...' if len(msg)>80 else ''}")
            try:
                text, tools, errors = await send_message(client, sid, msg)

                all_tool_calls.extend(tools)
                if tools:
                    for t in tools:
                        print(f"  → tool_call: {str(t)[:100]}")

                if errors:
                    result["error"] = errors[0]
                    print(f"  ❌ ERROR: {errors[0]}")
                    return result

                short = text[:300].replace("\n", " ")
                print(f"  → AI: {short}{'...' if len(text)>300 else ''}")
                result["final_response"] = text

            except Exception as exc:
                result["error"] = str(exc)
                print(f"  ❌ EXCEPTION: {exc}")
                return result

    # Require both calc_complete event AND "计算完成" in the final response
    calc_complete_fired = any("[calc_complete]" in tc for tc in all_tool_calls)
    has_result_text = any(kw in result["final_response"] for kw in ["计算完成", "kg CO₂e", "kgCO2e"])
    if calc_complete_fired and has_result_text:
        result["passed"] = True
        print(f"\n  ✅ PASSED — calculation completed")
    else:
        result["passed"] = False
        if not calc_complete_fired:
            print(f"\n  ❌ FAILED — calc_complete event never fired (calculation not triggered)")
        else:
            print(f"\n  ❌ FAILED — calc_complete fired but no result text in response")

    return result


async def main():
    print("\n" + "="*60)
    print("  Carbon Calculation Full-Cycle Test Suite")
    print("="*60)

    # Verify server
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE_URL}/provider", timeout=5)
        info = r.json()
        print(f"\n  Provider: {info['provider']} / {info['model']}")

    results = []
    for test in TESTS:
        # Check server is alive before each test
        try:
            async with httpx.AsyncClient() as c:
                await c.get(f"{BASE_URL}/provider", timeout=3)
        except Exception:
            print(f"\n⚠ Server unreachable before {test['name']} — waiting 8s for restart...")
            await asyncio.sleep(8)

        r = await run_test(test)
        results.append(r)
        await asyncio.sleep(3)  # brief pause between tests

    # Summary
    print(f"\n\n{'='*60}")
    print("  SUMMARY")
    print(f"{'='*60}")
    passed = sum(1 for r in results if r["passed"])
    failed = len(results) - passed
    for r in results:
        status = "✅ PASS" if r["passed"] else "❌ FAIL"
        note = f" — {r['error']}" if r["error"] else ""
        print(f"  {status}  {r['name']}{note}")

    print(f"\n  {passed}/{len(results)} tests passed, {failed} failed")


if __name__ == "__main__":
    asyncio.run(main())
