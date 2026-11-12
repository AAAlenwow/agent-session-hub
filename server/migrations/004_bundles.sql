-- 004: 原件层支持「打包体」（WorkBuddy 的 jsonl + 旁挂 tool-results）
--      并把唯一键从 (user_id, ext_id, sha256) 改成 (user_id, ext_id)：
--      同一个会话反复同步应该是覆盖最新版，而不是每次内容变化就多一行。
--
-- 升级方式（已有 v3 部署）：
--   docker compose exec -T postgres psql -U agenthub -d agenthub < server/migrations/004_bundles.sql

alter table transcripts add column if not exists bundle boolean not null default false;
alter table transcripts add column if not exists members int not null default 0;
-- 活跃时长：跨天长会话的首尾差没有意义，只累计 ≤30min 的相邻间隔
alter table transcripts add column if not exists active_seconds int not null default 0;

-- 先清掉历史重复（同会话多版本只保留最新一条），否则唯一索引建不起来
delete from transcripts t
using transcripts k
where t.user_id = k.user_id
  and t.ext_id = k.ext_id
  and t.id < k.id;

alter table transcripts drop constraint if exists transcripts_user_id_ext_id_sha256_key;
create unique index if not exists uq_transcripts_user_ext on transcripts (user_id, ext_id);
