-- 本脚本在 POSTGRES_DB 指定的业务库（homepilot）中执行。
-- 第 1 周只确认 pgvector 扩展可用，不创建向量表。
CREATE EXTENSION IF NOT EXISTS vector;
