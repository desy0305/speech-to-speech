from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


APP_DIR = Path(__file__).resolve().parents[1] / "apps" / "hf-realtime-voice-space"
sys.path.insert(0, str(APP_DIR))
SPEC = importlib.util.spec_from_file_location("hf_voice_server", APP_DIR / "server.py")
assert SPEC and SPEC.loader
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


def _tool_result(payload: object) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


def test_memory_recall_queries_expand_broad_family_question():
    queries = server._memory_recall_queries("Какво помниш за мен и семейството ми?", [], "bg")

    assert "Lazar" in queries
    assert "Mateevi family" in queries
    assert "Семейство Матееви" in queries
    assert "Какво помниш за мен и семейството ми?" not in queries


@pytest.mark.asyncio
async def test_memory_recall_fans_out_and_finds_lazar(monkeypatch):
    seen_queries: list[str] = []

    async def fake_mcp_requests(requests, *, timeout_s=server.MCP_REQUEST_TIMEOUT_S):
        results = []
        for method, params in requests:
            assert method == "tools/call"
            name = params["name"]
            arguments = params["arguments"]
            if name == "search_nodes":
                query = arguments["query"]
                seen_queries.append(query)
                if query == "Lazar":
                    results.append(
                        _tool_result(
                            {
                                "entities": [
                                    {
                                        "type": "entity",
                                        "entityType": "Person",
                                        "name": "Lazar",
                                        "observations": ["User's name is Lazar.", "Lazar lives in Plovdiv."],
                                    }
                                ],
                                "relations": [
                                    {"type": "relation", "from": "Lazar", "relationType": "lives in", "to": "Plovdiv"}
                                ],
                            }
                        )
                    )
                else:
                    results.append(_tool_result({"entities": [], "relations": []}))
            elif name == "open_nodes":
                results.append(_tool_result({"entities": [], "relations": []}))
            else:
                raise AssertionError(f"unexpected tool {name}")
        return results

    monkeypatch.setattr(server, "_mcp_requests", fake_mcp_requests)

    result = await server.memory_recall(
        server.MemoryRecallRequest(query="What do you remember about me and my family?", language="en")
    )

    assert result["found"] is True
    assert "Lazar" in seen_queries
    assert result["entities"][0]["name"] == "Lazar"
    assert "User's name is Lazar." in result["summary"]


@pytest.mark.asyncio
async def test_memory_remember_skips_duplicate_observations(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_mcp_requests(requests, *, timeout_s=server.MCP_REQUEST_TIMEOUT_S):
        results = []
        for method, params in requests:
            assert method == "tools/call"
            calls.append((params["name"], params["arguments"]))
            if params["name"] == "open_nodes":
                results.append(
                    _tool_result(
                        {
                            "entities": [
                                {
                                    "type": "entity",
                                    "entityType": "Person",
                                    "name": "Lazar",
                                    "observations": ["User's name is Lazar."],
                                }
                            ],
                            "relations": [],
                        }
                    )
                )
            elif params["name"] == "add_observations":
                results.append(_tool_result([{"entityName": "Lazar", "addedObservations": ["Lazar likes testing."]}]))
            else:
                results.append(_tool_result({}))
        return results

    monkeypatch.setattr(server, "_mcp_requests", fake_mcp_requests)

    result = await server.memory_remember(
        server.MemoryRememberRequest(
            observations=[
                {
                    "entityName": "Lazar",
                    "contents": ["User's name is Lazar.", "Lazar likes testing."],
                }
            ]
        )
    )

    add_calls = [arguments for name, arguments in calls if name == "add_observations"]
    assert result["ok"] is True
    assert result["skippedDuplicateObservations"] == [
        {"entityName": "Lazar", "contents": ["User's name is Lazar."]}
    ]
    assert add_calls == [{"observations": [{"entityName": "Lazar", "contents": ["Lazar likes testing."]}]}]
