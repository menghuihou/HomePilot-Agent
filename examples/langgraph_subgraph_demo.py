"""LangGraph 子图挂载最小示例：2 节点子图挂进主图的状态映射。

对应第 6 周实施计划「第 1 步：学习 spike」的第 1 件事。

运行方式：uv run python examples/langgraph_subgraph_demo.py

跑完后能回答三个问题：
1. 子图挂载为什么只需要主图加一个节点（或一条条件边）？
   子图自己就是一张编译好的图，主图把它当普通节点用——进不进它由
   条件边决定，子图内部有几个节点、怎么连，主图一概不可见。
2. add_node("sub", subgraph.compile()) 的 schema 约束是什么？
   直接挂载时父图把整个 State 传给子图、子图的增量更新合并回父图，
   两边键名必须能对上；父图多出来的键原样流过、互不干扰。
3. 什么时候必须用 wrapper 节点？
   父子 schema 键名对不上时（如第 6 周的 AgentState vs HomeModeState），
   写一个普通函数节点手动挑字段：父图字段 → 子图输入 → 子图输出 → 父图字段。
"""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

# =====================================================================
# 第 1 部分：直接挂载——父子 State 键名重叠，add_node 直接收编译后的子图
# =====================================================================


class HomeModeState(TypedDict):
    """子图的状态：只描述模式迁移自己关心的字段。"""

    home_id: str
    target_mode: str
    planned_changes: list[dict]
    blocked_changes: list[dict]


def plan(state: HomeModeState) -> dict:
    """按目标模式生成迁移计划（spike 用写死的两步，真实实现在第 3 步纯函数）。"""
    planned = [
        {"device_id": "light-1", "action": "power_off", "risk": "low"},
        {
            "device_id": "ac-1",
            "action": "set_temperature",
            "target": "eco",
            "risk": "medium",
        },
    ]
    return {"planned_changes": planned}


def dry_run(state: HomeModeState) -> dict:
    """冲突检测：spike 里固定把 ac-1 标记为待确认，验证 blocked 字段能流回父图。"""
    blocked = [{"device_id": "ac-1", "reason": "窗户开着，暂停关空调，标记待确认"}]
    return {"blocked_changes": blocked}


sub_builder = StateGraph(HomeModeState)
sub_builder.add_node(plan)
sub_builder.add_node(dry_run)
sub_builder.add_edge(START, "plan")
sub_builder.add_edge("plan", "dry_run")
sub_builder.add_edge("dry_run", END)
home_mode_subgraph = sub_builder.compile()


class MainState(HomeModeState):
    """父图状态：继承子图的全部键（键名重叠的最简做法）+ 父图自己的字段。"""

    chat_history: list[str]


main_builder = StateGraph(MainState)
# 直接挂载：编译后的子图就是一个节点。父图的 chat_history 不在子图
# schema 里，子图不读它，最终也会原样保留——多余键流过、互不干扰。
main_builder.add_node("home_mode", home_mode_subgraph)
main_builder.add_edge(START, "home_mode")
main_builder.add_edge("home_mode", END)
main_graph = main_builder.compile()

final = main_graph.invoke(
    {
        "home_id": "home-1",
        "target_mode": "away",
        "planned_changes": [],
        "blocked_changes": [],
        "chat_history": ["我出门了"],
    }
)
print("== 第 1 部分：直接挂载（键名重叠）==")
print("planned_changes:", final["planned_changes"])
print("blocked_changes:", final["blocked_changes"])
print("父图多余键 chat_history 原样保留:", final["chat_history"])
print()


# =====================================================================
# 第 2 部分：wrapper 挂载——父子 schema 键名对不上，手动做字段映射
# =====================================================================


class AgentState(TypedDict):
    """主图状态：和子图的键完全不同——直接挂载会拿不到数据，必须写 wrapper。"""

    messages: list[str]
    intent: str
    mode_result: dict


def run_home_mode(state: AgentState) -> dict:
    """wrapper 节点：主图字段 → 子图输入 → 子图输出 → 主图字段。"""
    # 1) 主图 → 子图：只挑子图需要的键，不把整包塞进去。
    sub_input: HomeModeState = {
        "home_id": "home-1",
        "target_mode": "sleep",
        "planned_changes": [],
        "blocked_changes": [],
    }
    # 2) 运行子图：对主图来说这就是一次普通函数调用。
    sub_output = home_mode_subgraph.invoke(sub_input)
    # 3) 子图 → 主图：只回写主图关心的键（增量更新）。
    return {
        "mode_result": {
            "planned": sub_output["planned_changes"],
            "blocked": sub_output["blocked_changes"],
        },
        "messages": [f"已生成 {sub_input['target_mode']} 模式迁移计划"],
    }


wrapper_builder = StateGraph(AgentState)
wrapper_builder.add_node("run_home_mode", run_home_mode)
wrapper_builder.add_edge(START, "run_home_mode")
wrapper_builder.add_edge("run_home_mode", END)
wrapper_graph = wrapper_builder.compile()

final = wrapper_graph.invoke(
    {"messages": ["准备睡觉"], "intent": "home_mode", "mode_result": {}}
)
print("== 第 2 部分：wrapper 挂载（键名不同，手动映射）==")
print("messages:", final["messages"])
print("mode_result:", final["mode_result"])
