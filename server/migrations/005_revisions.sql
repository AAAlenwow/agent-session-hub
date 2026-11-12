-- 005: 版本历史（git 式提交 / diff / 回滚）
--
-- 每次同步 = 一次提交，写一行 transcript_revisions（含内容快照，可回滚）。
-- transcripts.rev 是当前 HEAD 版本号，同步时 +1。
--
-- 升级方式（已有部署）：
--   docker compose exec -T postgres psql -U agenthub -d agenthub < server/migrations/005_revisions.sql

alter table transcripts add column if not exists rev int not null default 1;

create table if not exists transcript_revisions (
  id                    bigserial primary key,
  transcript_id         bigint not null references transcripts(id) on delete cascade,
  rev                   int not null,
  user_id               bigint not null references users(id),
  message               text not null default '',
  sha256                text not null,
  content_gz            bytea not null,
  bytes                 int not null default 0,
  lines                 int not null default 0,
  input_tokens          bigint not null default 0,
  output_tokens         bigint not null default 0,
  cache_read_tokens     bigint not null default 0,
  diff_added            int not null default 0,
  diff_removed          int not null default 0,
  diff                  jsonb not null default '{}'::jsonb,
  created_at            timestamptz not null default now(),
  unique (transcript_id, rev)
);
create index if not exists idx_revisions_t on transcript_revisions (transcript_id, rev desc);

-- 已有数据：为现有 HEAD 补一条 rev=1 的快照，历史从此开始记
insert into transcript_revisions
  (transcript_id, rev, user_id, message, sha256, content_gz, bytes, lines,
   input_tokens, output_tokens, cache_read_tokens)
select t.id, 1, t.user_id, '（升级前已存在，历史基线）', t.sha256, t.content_gz,
       t.bytes, t.lines, t.input_tokens, t.output_tokens, t.cache_read_tokens
from transcripts t
where not exists (select 1 from transcript_revisions r where r.transcript_id = t.id);
