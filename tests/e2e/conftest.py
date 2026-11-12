"""E2E 场景测试的基座：真实 Docker 栈 + 真实 HTTP，不 mock 任何东西。

栈没起时整目录自动 skip，所以 `pytest tests`（纯单测）仍然可跑。
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile
import urllib.parse
import uuid

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import helpers as H  # noqa: E402

RUN = os.environ.get("AGENT_HUB_E2E_RUN") or uuid.uuid4().hex[:6]


@pytest.fixture(scope="session")
def hub() -> H.Api:
    if not H.alive():
        pytest.skip(f"agent-hub 测试栈未启动（{H.BASE_URL}）；先跑 tests/e2e/run_e2e.sh up")
    return H.Api()


@pytest.fixture(scope="session")
def admin(hub: H.Api) -> H.Api:
    r = hub.get("/v1/sessions", token=H.ADMIN_TOKEN)
    if r.status != 200:
        pytest.fail(f"ADMIN_TOKEN 无效（{r.status}）：检查 tests/e2e/.env.e2e 与容器环境")
    return hub.with_token(H.ADMIN_TOKEN)


@pytest.fixture(scope="session")
def people(admin: H.Api) -> dict:
    """三名成员：alice（普通）、bob（普通，交接前一任）、carol（普通，接任者）。"""
    out = {}
    for name in ("alice", "bob", "carol"):
        full = f"{name}-{RUN}"
        r = admin.post("/v1/admin/users?name=" + urllib.parse.quote(full))
        assert r.status == 200, r
        d = r.json()
        out[name] = {"name": full, "token": d["token"], "id": d["id"], "api": admin.with_token(d["token"])}
    return out


@pytest.fixture(scope="session")
def sandbox():
    """临时目录。不用 pytest 的 tmp_path：本机沙箱对 /private/var 的 mkdir 有干扰。"""
    d = tempfile.mkdtemp(prefix="ahe2e-")
    yield pathlib.Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="session")
def db():
    if not H.sql_ok():
        pytest.skip("无法通过 docker compose exec 访问测试数据库")
    return H.sql


def uid(prefix: str = "x") -> str:
    """每次调用生成不冲突的短 id（同一次运行内多份数据互不干扰）。"""
    return f"{prefix}-{RUN}-{uuid.uuid4().hex[:6]}"
