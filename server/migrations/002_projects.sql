-- 升级脚本：v1 → v2（项目 / 负责人交接追踪）
-- 仅适用于已用 v1 schema 初始化过的部署；新部署由 01_schema.sql 自动建全。
-- 用法：docker compose exec -T postgres psql -U agenthub -d agenthub < server/migrations/002_projects.sql

create table if not exists projects (
  id          bigserial primary key,
  name        text not null unique,
  repo        text not null default '',
  description text not null default '',
  created_at  timestamptz not null default now()
);

create table if not exists project_owners (
  id          bigserial primary key,
  project_id  bigint not null references projects(id) on delete cascade,
  user_id     bigint not null references users(id),
  role        text not null default 'owner',
  assigned_at timestamptz not null default now(),
  released_at timestamptz
);
create index if not exists idx_project_owners on project_owners (project_id, assigned_at desc);

alter table sessions add column if not exists project_id bigint references projects(id);
create index if not exists idx_sessions_project on sessions (project_id);
