-- 007: 机器溯源
-- 会话记录从哪台机器 / 哪个服务器上传（hostname 或 AGENT_HUB_MACHINE 自定义标签）。
-- 手机端 / 多机场景靠它回答「这条会话是哪台机器上的活」，是幂等迁移。

alter table sessions add column if not exists machine text not null default '';
create index if not exists idx_sessions_machine on sessions (machine);

-- 原件层同样记机器名：wb-sync 只传原件不报事件，没有 session 行可反查
alter table transcripts add column if not exists machine text not null default '';

-- 老数据无法回溯真实来源，留空串；客户端下次上报会自动补上（ingest 的 on conflict
-- 用 coalesce(nullif(excluded.machine,''), sessions.machine) 保留已有值）。
