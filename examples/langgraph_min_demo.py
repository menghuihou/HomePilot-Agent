"""LangGraph 最小示例：State / Node / Edge / checkpointer / interrupt / Command。

运行方式：uv run python examples/langgraph_min_demo.py
"""

from operator import add
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


# 1) State：整张图共享的数据结构。
#    Annotated 的第二个参数是 reducer：规定"多个节点对同一字段的更新如何合并"。
#    这里用 add：各节点返回的 list 会被拼接，而不是互相覆盖。
class State(TypedDict):
    messages: Annotated[list[str], add]
    approved: bool


# 2) Node：一个普通函数，入参是当前 State，返回值是"增量更新"（只写想改的字段）。
def propose(state: State) -> dict:
    return {"messages": ["Agent: 建议把客厅灯调到 50%，是否执行？"]}


def human_approval(state: State) -> dict:
    # 4) interrupt：节点执行到这里时整张图"挂起"。
    #    - 第一次跑到这里：把参数抛给外部调用方，图停住；
    #    - 恢复跑到这里：interrupt() 的返回值就是外部 Command(resume=...) 传入的内容。
    answer = interrupt("请人工审批：yes / no")
    return {"messages": [f"Human: {answer}"], "approved": answer == "yes"}


def execute(state: State) -> dict:
    return {"messages": ["Agent: 已执行"]}


# 3) Edge：把节点连成有向图。
#    普通边固定跳转；add_conditional_edges 按 State 动态选分支。
builder = StateGraph(State)
builder.add_node(propose)
builder.add_node(human_approval)
builder.add_node(execute)
builder.add_edge(START, "propose")
builder.add_edge("propose", "human_approval")
builder.add_conditional_edges(
    "human_approval",
    lambda s: "execute" if s["approved"] else END,
)
builder.add_edge("execute", END)

# 5) checkpointer：图的"存档器"。每个超步（节点执行后）都会把 State 快照
#    按 thread_id 存进 checkpointer。interrupt 挂起后的状态就存在这里；
#    换成 SqliteSaver/PostgresSaver 后可以跨进程、跨重启恢复。
graph = builder.compile(checkpointer=MemorySaver())


def run_thread(thread_id: str, resume_answer: str) -> None:
    # thread_id 标识一次独立的对话/流程；同一个 checkpointer 里不同 thread 互不干扰。
    config = {"configurable": {"thread_id": thread_id}}

    result = graph.invoke({"messages": [], "approved": False}, config)
    print(f"[{thread_id}] 挂起时返回 keys: {sorted(result.keys())}")
    print(f"[{thread_id}] 挂起在下一段的节点(next): {graph.get_state(config).next}")

    # 6) Command(resume=...)：带上人工答复，从上次中断点恢复执行。
    final = graph.invoke(Command(resume=resume_answer), config)
    print(f"[{thread_id}] 恢复后最终 State: {final}")
    print()


# 同意 → 走 execute 分支；拒绝 → 条件边直接到 END，跳过 execute。
run_thread("thread-yes", "yes")
run_thread("thread-no", "no")
