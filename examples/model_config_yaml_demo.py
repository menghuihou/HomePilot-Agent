"""模型适配层配置加载最小示例：yaml → Pydantic 校验 → 角色映射。

对应第 6 周实施计划「第 1 步：学习 spike」的第 3 件事。

运行方式：uv run python examples/model_config_yaml_demo.py

跑完后能回答两个问题：
1. yaml 里该放什么、不该放什么？
   放「角色 → provider / 模型名」这类纯选择；不放密钥（走环境变量）、
   不放业务逻辑分支。配置是数据，代码里只写「怎么读、怎么校验」。
2. 为什么缺失 / 非法要快速失败而不是给默认值？
   模型配置错了，错的是整条 LLM 链路的行为；启动时失败一眼可见，
   运行中悄悄用错模型极难排查。

依赖说明：PyYAML 目前是传递依赖（随 langchain / streamlit 等进入环境），
第 8 步正式接入 llm_client.py 时应显式加入 pyproject dependencies。
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ValidationError, field_validator

DEMO_YAML = Path(__file__).resolve().with_name("models_demo.yaml")


class ModelSpec(BaseModel):
    """单个模型描述：provider 收敛为白名单，防止 yaml 里写错厂商名。"""

    provider: Literal["anthropic"] = "anthropic"
    model: str


class RoleConfig(BaseModel):
    """一个角色的主 / 备模型；fallback 可缺省（缺省时降级路径由调用方决定）。"""

    primary: ModelSpec
    fallback: ModelSpec | None = None


class ModelConfig(BaseModel):
    """整份配置：角色映射 + 默认角色必须真实存在。"""

    default_role: str = "light"
    roles: dict[str, RoleConfig]

    @field_validator("roles")
    @classmethod
    def _default_role_must_exist(cls, roles: dict, info) -> dict:
        default_role = info.data.get("default_role")
        if default_role and default_role not in roles:
            raise ValueError(f"default_role '{default_role}' 不在 roles 里")
        return roles


def load_model_config(path: Path) -> ModelConfig:
    """加载并校验模型配置；文件缺失、结构不对、字段非法都快速失败。"""
    if not path.exists():
        raise FileNotFoundError(f"模型配置不存在：{path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError(f"模型配置必须是映射结构，实际是：{type(raw).__name__}")
    return ModelConfig.model_validate(raw)


def main() -> None:
    # 1) 快乐路径：读示例 yaml → Pydantic 校验 → 打印角色映射。
    config = load_model_config(DEMO_YAML)
    print("== 快乐路径：加载 models_demo.yaml ==")
    print(f"default_role: {config.default_role}")
    for role_name, role in config.roles.items():
        fallback = role.fallback.model if role.fallback else "无"
        print(f"  {role_name}: primary={role.primary.model}, fallback={fallback}")
    print(f"light 角色生效模型: {config.roles[config.default_role].primary.model}")

    # 2) 缺失文件：快速失败，报错信息能直接定位到路径。
    print("\n== 缺失文件：加载不存在的路径 ==")
    try:
        load_model_config(DEMO_YAML.with_name("not_exist.yaml"))
    except FileNotFoundError as exc:
        print(f"FileNotFoundError: {exc}")

    # 3) 非法内容：provider 不在白名单 → Pydantic 校验报错，字段级定位。
    print("\n== 非法内容：provider 写错 ==")
    bad_yaml = """
default_role: light
roles:
  light:
    primary:
      provider: openai
      model: gpt-4o
"""
    try:
        ModelConfig.model_validate(yaml.safe_load(bad_yaml))
    except ValidationError as exc:
        first = exc.errors()[0]
        print(
            f"ValidationError: {' -> '.join(str(p) for p in first['loc'])}: {first['msg']}"
        )

    # 4) 非法内容：default_role 指向不存在的角色 → 自定义校验快速失败。
    print("\n== 非法内容：default_role 不在 roles 里 ==")
    bad_default = """
default_role: flagship
roles:
  light:
    primary:
      provider: anthropic
      model: claude-sonnet-4-5
"""
    try:
        ModelConfig.model_validate(yaml.safe_load(bad_default))
    except ValidationError as exc:
        first = exc.errors()[0]
        print(
            f"ValidationError: {' -> '.join(str(p) for p in first['loc'])}: {first['msg']}"
        )


if __name__ == "__main__":
    main()
