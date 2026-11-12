-- 006: 占用锁修复 + 原件层项目归属
--
-- 背景（对抗式审查 P1-4 / P1-6）：
--   1) claim 无归属校验：任何成员能锁任何人的会话，且没有任何解铃手段（admin 也解不开）
--   2) 锁键是全局 (agent, ext_id) 不含用户：不同用户的同名 ext_id 互相挡锁，
--      配合 sessions 列表全员可见，可被用来批量锁死同事的会话
--      → 锁改按 transcript_id（服务端 API 已同步改为归属校验 + 新增 DELETE /claim）
--   3) 纯上传原件（wb-sync 不报事件）时 project 归属整条丢失 → transcripts 加 project 列
--
-- 新部署不需要本文件（schema.sql 已是合并后的终态）。

alter table transcripts add column if not exists project text not null default '';
create index if not exists idx_transcripts_project on transcripts (project);

-- 锁从 (agent, ext_id) 迁到 transcript_id：
--   旧锁按 (agent, ext_id) 反查所属 transcript；查不到的（对应会话已删）直接清掉
alter table resume_claims add column if not exists transcript_id bigint;
update resume_claims rc set transcript_id = t.id
  from transcripts t
  where rc.transcript_id is null and rc.agent = t.agent and rc.ext_id = t.ext_id;
delete from resume_claims where transcript_id is null;
alter table resume_claims drop constraint if exists resume_claims_agent_ext_id_key;
alter table resume_claims alter column transcript_id set not null;
alter table resume_claims add unique (transcript_id);
