import json
from datetime import UTC, datetime, timedelta

import httpx

from ha_llm_context.ask import ask, build_messages
from ha_llm_context.collect import HAClient, runs_in_window
from ha_llm_context.config import HASettings, LLMSettings, load_dotenv, parse_when

START = datetime(2026, 3, 7, 18, tzinfo=UTC)
END = datetime(2026, 3, 7, 20, tzinfo=UTC)


def test_history_request_keeps_attributes():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json=[])

    client = HAClient(HASettings("http://ha.invalid:8123", "test-token"), httpx.MockTransport(handler))
    client.history(START, END, ["light.fake", "sensor.fake"])
    assert seen["path"] == "/api/history/period/2026-03-07T18:00:00+00:00"
    assert seen["params"]["filter_entity_id"] == "light.fake,sensor.fake"
    assert "minimal_response" not in seen["params"]
    assert seen["params"]["significant_changes_only"] == "0"
    assert seen["auth"] == "Bearer test-token"


def test_client_only_issues_get_requests():
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, json={})

    client = HAClient(HASettings("http://ha.invalid", "t"), httpx.MockTransport(handler))
    client.history(START, END, ["light.fake"])
    client.logbook(START, END)
    client.error_log()
    client.state("automation.fake")
    assert set(methods) == {"GET"}


def test_runs_in_window():
    def run(rid, minutes):
        return {"run_id": rid, "timestamp": {"start": (START + timedelta(minutes=minutes)).isoformat()}}

    kept = runs_in_window([run("late", 200), run("b", 30), run("early", -5), run("a", 10)], START, END)
    assert [r["run_id"] for r in kept] == ["a", "b"]


def test_parse_when():
    now = datetime(2026, 3, 7, 20, tzinfo=UTC)
    assert parse_when("2h", now) == START
    assert parse_when("90m", now) == now - timedelta(minutes=90)
    assert parse_when("2026-03-07T18:00:00+00:00") == START


def test_load_dotenv_does_not_override(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nHA_URL="http://from-file"\nexport LLM_MODEL=fake-model\n')
    monkeypatch.setenv("HA_URL", "http://from-env")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    load_dotenv(env)
    import os
    assert os.environ["HA_URL"] == "http://from-env"
    assert os.environ["LLM_MODEL"] == "fake-model"


def test_build_messages_orders_context_timeline_question(tmp_path):
    ctx = tmp_path / "hallway.yaml"
    ctx.write_text("alias: Fake automation\n")
    msgs = build_messages("# Home Assistant timeline\n...", "Why didn't it run?", [ctx])
    user = msgs[1]["content"]
    assert msgs[0]["role"] == "system"
    assert user.index("hallway.yaml") < user.index("# Home Assistant timeline") < user.index("Why didn't")


def test_ask_posts_openai_compatible_request():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "It's dark enough."}}]})

    settings = LLMSettings("http://llm.invalid/v1", "fake-model")
    out = ask(settings, [{"role": "user", "content": "hi"}], transport=httpx.MockTransport(handler))
    assert out == "It's dark enough."
    assert seen["url"] == "http://llm.invalid/v1/chat/completions"
    assert seen["body"]["model"] == "fake-model"
    assert seen["auth"] is None


class FakeWS:
    def __init__(self, results):
        self.results, self.calls = results, []

    def call(self, msg_type, **fields):
        self.calls.append(msg_type)
        result = self.results[msg_type]
        if isinstance(result, Exception):
            raise result
        return result


def test_error_log_404_falls_back_to_system_log(tmp_path):
    from ha_llm_context.collect import collect_errors

    client = HAClient(HASettings("http://ha.invalid", "t"),
                      httpx.MockTransport(lambda r: httpx.Response(404)))
    ws = FakeWS({"system_log/list": [{"name": "x", "level": "ERROR"}]})
    notes = collect_errors(client, ws, tmp_path)
    assert ws.calls == ["system_log/list"]
    assert json.loads((tmp_path / "system_log.json").read_text())[0]["name"] == "x"
    assert not (tmp_path / "error_log.txt").exists()
    assert "404" in notes[0]


def test_error_log_missing_entirely_does_not_fail(tmp_path):
    from ha_llm_context.collect import collect_errors

    client = HAClient(HASettings("http://ha.invalid", "t"),
                      httpx.MockTransport(lambda r: httpx.Response(404)))
    notes = collect_errors(client, FakeWS({"system_log/list": RuntimeError("unauthorized")}), tmp_path)
    assert len(notes) == 2 and "no error log" in notes[1]
