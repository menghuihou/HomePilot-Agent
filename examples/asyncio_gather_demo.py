"""asyncio 最小示例：gather 并发 / 单任务超时 / return_exceptions / 结果保序。

对应第 4 周实施计划「第 1 步：asyncio 并发学习 spike」。

运行方式：uv run python examples/asyncio_gather_demo.py

跑完后能回答三个问题：
1. 为什么 gather 并发 8 个查询的总耗时 ≈ 最慢那个，而不是累加？
2. 某个协程超时/抛异常时，gather 默认行为是什么？return_exceptions=True 后呢？
3. gather 的返回结果和传入顺序是什么关系？
"""

import asyncio
import time


# 1) 模拟单设备状态查询：sleep 代表真实的 MCP 工具调用耗时。
async def query_device(device_id: int, cost: float) -> dict:
    await asyncio.sleep(cost)
    return {"device_id": device_id, "online": True, "cost": cost}


# 2) 演示一：8 个查询并发跑，总耗时 ≈ 最慢的单个查询，而不是 8 个耗时的累加。
async def demo_concurrent_timing() -> None:
    print("=" * 60)
    print("演示一：gather 并发耗时（8 台设备，各自 0.2~1.0 秒）")

    costs = [0.2, 0.4, 1.0, 0.3, 0.5, 0.6, 0.8, 0.7]
    start = time.perf_counter()
    results = await asyncio.gather(
        *(query_device(i, cost) for i, cost in enumerate(costs))
    )
    elapsed = time.perf_counter() - start

    print(f"单个查询耗时合计（串行的话）：{sum(costs):.1f} 秒")
    print(f"最慢单个查询：{max(costs):.1f} 秒")
    print(f"gather 并发实际耗时：{elapsed:.2f} 秒  ← 应接近 1.0 而不是 4.5")
    print(f"拿到 {len(results)} 条结果\n")


# 3) 演示二：某个协程超时/抛异常时的两种 gather 行为。
async def query_slow_device(device_id: int) -> dict:
    # 一台"卡死"的设备：永远查不完，等 asyncio.timeout(0.5) 来收割。
    await asyncio.sleep(10)
    return {"device_id": device_id, "online": True}


async def query_one_with_timeout(device_id: int, cost: float) -> dict:
    """单设备查询的安全封装：超时在这里消化成结果值，不向外抛异常。

    这是巡检子图 _query_one 的雏形：超时必须包在每个任务内部，
    而不是只包整体 gather——否则一台设备卡死会拖住全部。
    """
    try:
        async with asyncio.timeout(0.5):
            return await query_device(device_id, cost)
    except TimeoutError:
        # 把失败降级为普通结果值，gather 就永远收不到异常。
        return {"device_id": device_id, "status": "query_failed", "reason": "timeout"}


async def demo_timeout() -> None:
    print("=" * 60)
    print("演示二：超时处理（设备 1 正常 0.3 秒，设备 2 卡死 10 秒）")

    # 行为 A：不封超时也不接异常 → 设备 2 的任务永不返回，gather 卡死。
    # 这里用 wait_for(整体, 1 秒) 模拟"调用方等不到只能放弃"。
    try:
        await asyncio.wait_for(
            asyncio.gather(query_device(1, 0.3), query_slow_device(2)),
            timeout=1.0,
        )
    except TimeoutError:
        print("行为 A（无超时封装）：gather 整体被卡死，调用方 1 秒后被迫放弃")

    # 行为 B：单任务内包 asyncio.timeout，超时降级为结果值 → gather 正常收齐。
    results = await asyncio.gather(
        query_one_with_timeout(1, 0.3),
        query_one_with_timeout(2, 10.0),
    )
    print(f"行为 B（任务内超时封装）：gather 正常返回 {results}\n")


# 4) 演示三：return_exceptions —— 异常不炸整体，而是"占位"在结果列表里。
async def query_broken_device(device_id: int) -> dict:
    raise RuntimeError(f"设备 {device_id} MCP 调用崩溃")


async def demo_return_exceptions() -> None:
    print("=" * 60)
    print("演示三：return_exceptions（设备 1 / 3 正常，设备 2 崩溃）")

    # 默认：第一个异常直接抛给调用方，设备 3 的结果丢失。
    # 注意：协程对象只能被 await 一次，两次演示各自重建任务列表。
    try:
        await asyncio.gather(
            query_device(1, 0.2), query_broken_device(2), query_device(3, 0.3)
        )
    except RuntimeError as exc:
        print(f"默认（不加参数）：异常直接炸出 → {exc}，设备 3 的结果拿不到了")

    # return_exceptions=True：异常对象占位在结果列表对应位置，保住其余结果。
    results = await asyncio.gather(
        query_device(1, 0.2),
        query_broken_device(2),
        query_device(3, 0.3),
        return_exceptions=True,
    )
    print("return_exceptions=True：异常占位在原位置，其余结果完整：")
    for i, item in enumerate(results, start=1):
        kind = "异常对象" if isinstance(item, BaseException) else "正常结果"
        print(f"  位置 {i} ← {kind}: {item!r}")
    print()


# 5) 演示四：gather 严格保序 —— 结果[i] 对应传入的第 i 个协程，与完成先后无关。
async def demo_order_preserved() -> None:
    print("=" * 60)
    print("演示四：结果保序（越慢的设备排在越后面完成）")

    # 故意让后传入的设备先完成，观察结果位置是否被打乱。
    costs = [1.0, 0.1, 0.5, 0.2]
    results = await asyncio.gather(
        *(query_device(i, cost) for i, cost in enumerate(costs))
    )
    for item in results:
        print(f"  位置 {item['device_id']}（耗时 {item['cost']} 秒）← 与传入顺序一致")
    print("\n结论：按传入顺序 zip 回设备即可，不要按完成时间重排。")


async def main() -> None:
    await demo_concurrent_timing()
    await demo_timeout()
    await demo_return_exceptions()
    await demo_order_preserved()


if __name__ == "__main__":
    asyncio.run(main())
