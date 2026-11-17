-- agent-session-hub schema (Postgres 14+)
create extension if not exists pg_trgm;

create table if not exists users (
  id          bigserial primary key,
  name        text not null unique,
  token_hash  text not null unique,        -- sha256(token)，不存明文
  is_admin    boolean not null default false,
  created_at  timestamptz not null default now()
);

create table if not exists sessions (
  id          bigserial primary key,
  user_id     bigint not null references users(id),
  ext_id      text not null,               -- CLI 侧的 session_id
  agent       text not null default 'claude-code',  -- claude-code / codex / glm / minimax ...
  project     text not null default '',    -- 仓库名
  machine     text not null default '',    -- 上传机器名（hostname / 自定义标签）
  cwd         text not null default '',
  repo        text not null default '',    -- git remote
  title       text not null default '',
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (user_id, ext_id)
);

create table if not exists events (
  id          bigserial primary key,
  session_id  bigint not null references sessions(id) on delete cascade,
  ts          timestamptz not null default now(),
  kind        text not null,               -- user_prompt / tool_call / turn_end / session_start ...
  summary     text not null default '',    -- 一行摘要，检索主字段
  files       jsonb not null default '[]',
  payload     jsonb not null default '{}'
);

create index if not exists idx_events_session   on events (session_id, ts);
create index if not exists idx_events_kind      on events (kind);
create index if not exists idx_events_summary_trgm on events using gin (summary gin_trgm_ops);
create index if not exists idx_sessions_updated on sessions (updated_at desc);

-- ---------- v2: 项目 / 负责人变更（交接）追踪 ----------

create table if not exists projects (
  id          bigserial primary key,
  name        text not null unique,       -- 仓库名（sessions.project 自动关联）
  repo        text not null default '',   -- git remote
  description text not null default '',
  created_at  timestamptz not null default now()
);

-- 负责人时间线：assign 新负责人时自动释放现任（released_at = now()），
-- 历任记录永久保留 —— 交接报告的数据来源
create table if not exists project_owners (
  id          bigserial primary key,
  project_id  bigint not null references projects(id) on delete cascade,
  user_id     bigint not null references users(id),
  role        text not null default 'owner',   -- owner / member
  assigned_at timestamptz not null default now(),
  released_at timestamptz                     -- null = 现任
);
create index if not exists idx_project_owners on project_owners (project_id, assigned_at desc);

alter table sessions add column if not exists project_id bigint references projects(id);
create index if not exists idx_sessions_project on sessions (project_id);

-- v7: 机器溯源（会话从哪台机器/服务器传上来的）；老库用 migrations/007_machine.sql
alter table sessions add column if not exists machine text not null default '';
create index if not exists idx_sessions_machine on sessions (machine);

-- 已有部署的升级方式（新库不需要，init 脚本会自动建全）：
--   docker compose exec -T postgres psql -U agenthub -d agenthub < server/migrations/002_projects.sql
--   docker compose exec -T postgres psql -U agenthub -d agenthub < server/migrations/003_transcripts.sql

-- ---------- v3: 会话原件归档（可跨机回放）+ 用量 ----------

create table if not exists transcripts (
  id                     bigserial primary key,
  session_id             bigint references sessions(id) on delete set null,
  user_id                bigint not null references users(id),
  agent                  text not null default 'claude-code',
  ext_id                 text not null,          -- CLI 侧 session id（resume 用）
  cwd                    text not null default '', -- 原始机器上的仓库绝对路径
  machine                text not null default '', -- 上传机器名（v7，原件层溯源）
  model                  text not null default '',
  bytes                  int not null default 0,
  lines                  int not null default 0,
  sha256                 text not null,
  content_gz             bytea not null,          -- 原始 JSONL 的 gzip
  input_tokens           bigint not null default 0,
  output_tokens          bigint not null default 0,
  cache_read_tokens      bigint not null default 0,
  cache_creation_tokens  bigint not null default 0,
  started_at             timestamptz,
  ended_at               timestamptz,
  active_seconds         int not null default 0,          -- 活跃时长：只累计 ≤30min 的相邻间隔
  bundle                 boolean not null default false,  -- true = tar.gz 打包体（含旁挂文件）
  members                int not null default 0,          -- 打包体内文件数
  rev                    int not null default 1,          -- 当前版本号（git 式提交号）
  project                text not null default '',        -- 项目名（纯上传路径没有 session 行时用它归属）
  created_at             timestamptz not null default now(),
  unique (user_id, ext_id)   -- 同一会话只留最新版：反复同步是覆盖，不是堆行
);

-- 版本历史：每次同步 = 一次提交。保留历史内容以便回滚（按 keep_revisions 裁剪）
create table if not exists transcript_revisions (
  id                    bigserial primary key,
  transcript_id         bigint not null references transcripts(id) on delete cascade,
  rev                   int not null,
  user_id               bigint not null references users(id),
  message               text not null default '',   -- 提交说明（用户填，可空）
  sha256                text not null,
  content_gz            bytea not null,
  bytes                 int not null default 0,
  lines                 int not null default 0,
  input_tokens          bigint not null default 0,
  output_tokens         bigint not null default 0,
  cache_read_tokens     bigint not null default 0,
  diff_added            int not null default 0,
  diff_removed          int not null default 0,
  diff                  jsonb not null default '{}'::jsonb,  -- 语义差异：新消息/工具/文件
  created_at            timestamptz not null default now(),
  unique (transcript_id, rev)
);
create index if not exists idx_revisions_t on transcript_revisions (transcript_id, rev desc);
create index if not exists idx_transcripts_user on transcripts (user_id, created_at desc);
create index if not exists idx_transcripts_ext on transcripts (ext_id);
create index if not exists idx_transcripts_project on transcripts (project);

-- 回放占用锁：同一会话同一时间只允许一台机器 resume。
-- 键是 transcript_id（而不是 agent+ext_id）：ext_id 只在单个用户内唯一，
-- 不同用户同名 ext_id 用全局键会互相挡锁。claim/release 都要求归属人或管理员。
create table if not exists resume_claims (
  id             bigserial primary key,
  transcript_id  bigint not null unique references transcripts(id) on delete cascade,
  agent          text not null,
  ext_id         text not null,
  user_id        bigint not null references users(id),
  claimed_at     timestamptz not null default now(),
  expires_at     timestamptz not null
);
