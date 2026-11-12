-- 升级脚本：v2 → v3（会话原件归档 + 回放占用锁）
-- 用法：docker compose exec -T postgres psql -U agenthub -d agenthub < server/migrations/003_transcripts.sql

create table if not exists transcripts (
  id                     bigserial primary key,
  session_id             bigint references sessions(id) on delete set null,
  user_id                bigint not null references users(id),
  agent                  text not null default 'claude-code',
  ext_id                 text not null,
  cwd                    text not null default '',
  model                  text not null default '',
  bytes                  int not null default 0,
  lines                  int not null default 0,
  sha256                 text not null,
  content_gz             bytea not null,
  input_tokens           bigint not null default 0,
  output_tokens          bigint not null default 0,
  cache_read_tokens      bigint not null default 0,
  cache_creation_tokens  bigint not null default 0,
  started_at             timestamptz,
  ended_at               timestamptz,
  created_at             timestamptz not null default now(),
  unique (user_id, ext_id, sha256)
);
create index if not exists idx_transcripts_user on transcripts (user_id, created_at desc);
create index if not exists idx_transcripts_ext on transcripts (ext_id);

create table if not exists resume_claims (
  id         bigserial primary key,
  agent      text not null,
  ext_id     text not null,
  user_id    bigint not null references users(id),
  claimed_at timestamptz not null default now(),
  expires_at timestamptz not null,
  unique (agent, ext_id)
);
