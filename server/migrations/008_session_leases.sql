-- 008: 会话执行权租约（v0.6.0）
-- 与 006 的 resume_claims 并存：claim 管「装一份来回放」，lease 管「我正在跑」。
-- 幂等迁移，可重复执行。

create table if not exists session_leases (
  session_key   text primary key,
  transcript_id bigint references transcripts(id) on delete set null,
  user_id       bigint not null references users(id),
  holder_id     text not null,
  machine       text not null default '',
  agent         text not null default '',
  mode          text not null default 'exec' check (mode in ('exec', 'read')),
  generation    bigint not null default 1,
  note          text not null default '',
  acquired_at   timestamptz not null default now(),
  renewed_at    timestamptz not null default now(),
  expires_at    timestamptz not null
);

create index if not exists idx_leases_active on session_leases (expires_at desc);
create index if not exists idx_leases_holder on session_leases (holder_id);

-- 老部署无需回填：没有租约就是「没人在跑」，语义正确。
