"""测试共享辅助：psycopg 连接串、原始 SQL 执行与审批行清理。

收敛自各测试文件的重复拷贝（原 _conninfo / _db_execute /
_cleanup_approvals 曾分散在 8 个文件）。数据库走真实本地库是项目的
刻意架构选择；本模块只服务测试数据构造与清理，应用路径不使用。
"""

import psycopg

from app.database import checkpoint_conninfo


def conninfo() -> str:
    """psycopg 原生 URI：委托应用侧共享实现（方言归一口径单一来源）。"""
    return checkpoint_conninfo()


def db_execute(query: str, params: tuple = (), fetch: bool = False):
    """原始 SQL 执行：测试数据构造与清理（提交后返回可选行集）。"""
    with psycopg.connect(conninfo()) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows


def cleanup_approvals(thread_id: str) -> None:
    """按 thread_id 清理审批审计行，保持开发库干净。"""
    db_execute("DELETE FROM approvals WHERE thread_id = %s", (thread_id,))
