-- Langfuse 使用独立数据库，与业务库 homepilot 逻辑隔离。
-- 初始化脚本以 POSTGRES_USER 执行，langfuse 库的 owner 也是该用户。
CREATE DATABASE langfuse;
