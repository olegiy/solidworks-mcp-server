"""Stage surface declarations remain authoritative through execution and comparison."""

import asyncio

import pytest

from tests.test_mesh_tools import ready_tools


def stage(ids, *, status=None):
    result = {"id": "base", "title": "Основа", "expected_result": "Основа готова", "mesh_region_ids": ids}
    if status:
        result.update(status=status, checks=[], tool_call_ids=[])
    return result


def test_started_stage_mesh_bindings_are_immutable_and_survive_validation():
    from cadnext.construction import set_plan, validate_saved_plan

    current = set_plan([], [stage(["surface:abc"])])
    current[0]["status"] = "done"
    with pytest.raises(ValueError, match="Начатый"):
        set_plan(current, [stage(["surface:other"])])
    restored, _ = validate_saved_plan(current, [])
    assert restored[0]["mesh_region_ids"] == ["surface:abc"]


def test_union_cannot_exceed_128_ids():
    from cadnext.construction import set_plan

    stages = [stage([f"region{i}" for i in range(128)]),
              {**stage(["extra"]), "id": "extra"}]
    with pytest.raises(ValueError, match="128"):
        set_plan([], stages)


async def test_only_current_issued_ids_can_bind_and_bytes_are_rechecked(tmp_path):
    from cadnext.mesh.contracts import MeshError

    tools, asset = await ready_tools(tmp_path)
    binding = tools.bind("s", {"asset_id": asset.asset_id, "tolerance_mm": .1}, run_id="run")
    try:
        page = await tools.dispatch(binding, "mesh_inspect", {}, stop=asyncio.Event())
        rid = page["regions"][0]["region_id"]
        await tools.validate_regions(binding, [rid])
        with pytest.raises(MeshError):
            await tools.validate_regions(binding, ["missing"])
        tools.store.path_for("s", asset.asset_id).write_bytes(b"changed")
        with pytest.raises(MeshError):
            await tools.validate_regions(binding, [rid])
    finally:
        await tools.aclose()


async def test_application_sends_required_ids_to_worker(tmp_path):
    tools, asset = await ready_tools(tmp_path)
    binding = tools.bind("s", {"asset_id": asset.asset_id, "tolerance_mm": .1}, run_id="run")

    class Worker:
        async def request(self, operation, payload, *, stop):
            assert operation == "compare"
            assert payload["options"]["required_region_ids"] == ["surface:required"]
            return {"sha256": asset.sha256, "frame_id": binding.reference.frame_id, "comparison": {}}

    tools.worker = Worker()
    await tools.compare(binding, {"path": "internal", "sha256": "cad", "chord_tolerance_mm": .01,
                                 "precision_known": False, "document_identity": "doc", "revision": 7},
                        stop=asyncio.Event(), cad_checks_passed=True, required_region_ids=["surface:required"])


@pytest.mark.parametrize("invalid", [False, True])
async def test_agent_plan_transfers_authoritative_ids_or_blocks_cad(tmp_path, invalid):
    import json

    from cadnext.agent import Agent
    from tests.fake_mcp import ok, tool
    from tests.test_agent import ScriptedProvider, reply
    from tests.test_staged_agent import SessionMcp

    tools, asset = await ready_tools(tmp_path)
    chosen = []

    def plan(messages):
        measurement = next(json.loads(m["content"]) for m in messages if m.get("tool_call_id") == "inspect")
        chosen[:] = ["missing" if invalid else measurement["value"]["regions"][0]["region_id"]]
        return reply(("plan", "set_construction_plan", {"stages": [stage(chosen)]}))

    original_compare = tools.compare
    compared = []

    async def compare(binding, snapshot, **kwargs):
        compared.append(kwargs["required_region_ids"])
        return await original_compare(binding, snapshot, **kwargs)

    tools.compare = compare
    import hashlib

    from cadnext.mesh.loader import load_mesh

    mesh = load_mesh(tools.store.path_for("s", asset.asset_id), format="obj", units="mm")
    raw = json.dumps({"vertices_mm": mesh.vertices.tolist(), "faces": mesh.faces.tolist()}).encode()
    path = tmp_path / "snapshot.json"
    path.write_bytes(raw)
    mcp = SessionMcp([("export_verification_mesh", ok({"snapshot": {"sha256": hashlib.sha256(raw).hexdigest(),
        "path": str(path), "precision_known": False, "chord_tolerance_mm": .01,
        "document_identity": "doc", "revision": 7}})),
        ("connect_solidworks", ok({"document_id": "doc", "document_update_stamp": 7}))])
    mcp.catalog.append(tool("export_verification_mesh"))
    provider = ScriptedProvider([reply(("inspect", "mesh_inspect", {})), plan,
                                 reply(("compare", "mesh_compare", {})), reply(content="Готово")])
    agent = Agent(mcp, mesh_tools=tools)
    try:
        run = agent.start(session_id="s", provider=provider, message="Сравни",
                          mesh_reference={"asset_id": asset.asset_id, "tolerance_mm": .1})
        await agent.wait(run.id)
        if invalid:
            assert not mcp.calls and not run.construction_plan and not compared
        else:
            assert compared == [chosen]
            assert run.mesh_verification["comparison"]["required_regions"][0]["complete"]
            assert run.mesh_verification["status"] == "unknown"
            replay = next(e for e in run.events if e["type"] == "mesh_verification")
            assert replay["data"]["coverage"]["reference_to_cad"]["complete"]
    finally:
        await agent.aclose()
