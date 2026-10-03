import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from miniagent.api import APIError, ChatClient
from miniagent.cli import main
from miniagent.config import Config
from miniagent.responses import ResponsesClient
from miniagent.security import Redactor
from miniagent.tools import SCHEMAS
from test_api import http_server, sse


class Auth:
    def __init__(self):
        self.redact = Redactor("synthetic-oauth", "synthetic-refresh")
        self.requests = 0

    def access_token(self):
        self.requests += 1
        return "synthetic-oauth"


def message(text="done", phase="final_answer"):
    return {"type": "message", "role": "assistant", "status": "completed", "phase": phase,
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def call(call_id="call_1", **changes):
    item = {"type": "function_call", "status": "completed", "call_id": call_id,
            "namespace": "miniagent", "name": "read_file", "arguments": '{"path":"example.txt"}'}
    item.update(changes)
    return item


def completed(*output, status="completed"):
    return {"type": "response.completed", "response": {"status": status, "output": list(output),
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}}


def client(url, auth=None):
    config = SimpleNamespace(**vars(Config(provider="chatgpt")))
    config.endpoint = url + "/responses"
    config.max_retries = 0
    return ResponsesClient(config, auth or Auth())


class ResponsesTests(unittest.TestCase):
    def test_empty_completion_is_not_a_successful_answer_or_automatically_retried(self):
        with http_server([{"body": sse(completed(), done=False)}]) as (url, requests):
            model = client(url)
            model.config.max_retries = 2
            with self.assertRaisesRegex(APIError, "no usable answer"):
                model.complete([])
            self.assertEqual(len(requests), 1)

    def test_transport_payload_native_phase_and_compaction_stream(self):
        with http_server([{"body": sse(completed(message()), done=False)}]) as (url, requests):
            auth = Auth()
            model = client(url, auth)
            fragments = []
            result = model.complete([{"role": "system", "content": "instructions"}, {"role": "user", "content": "hello"}],
                                    SCHEMAS, stream=False, on_text=fragments.append)
        request = requests[0]
        self.assertEqual(request["path"], "/v1/responses")
        self.assertEqual(request["headers"]["Authorization"], "Bearer synthetic-oauth")
        body = request["body"]
        self.assertTrue(body["stream"])
        self.assertFalse(body["store"])
        self.assertEqual(body["input"][0]["role"], "developer")
        self.assertEqual(body["tools"][0]["type"], "namespace")
        self.assertEqual(body["tools"][0]["tools"][0]["name"], SCHEMAS[0]["function"]["name"])
        self.assertFalse(body["tools"][0]["tools"][0]["strict"])
        self.assertTrue(set(body).isdisjoint({"max_output_tokens", "max_tokens", "previous_response_id", "temperature", "stream_options"}))
        self.assertEqual(result["choices"][0]["message"]["phase"], "final_answer")
        self.assertEqual(result["usage"]["prompt_tokens"], 10)
        self.assertEqual("".join(fragments), "[final_answer]\ndone")
        self.assertEqual(auth.requests, 1)

    def test_tool_roundtrip_and_encrypted_reasoning(self):
        reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque-reasoning"}
        with http_server([{"body": sse(completed(reasoning, message("looking", "commentary"), call()), done=False)}]) as (url, _):
            model = client(url)
            result = model.complete([])
        assistant = result["choices"][0]["message"]
        self.assertEqual(assistant["tool_calls"][0]["id"], "call_1")
        self.assertEqual(assistant["responses_reasoning"], [reasoning])
        history = [assistant, {"role": "tool", "tool_call_id": "call_1", "content": "file contents"}]
        original = copy.deepcopy(history)
        items = model._payload(history, SCHEMAS, True)["input"]
        self.assertEqual(items[0], reasoning)
        self.assertEqual(items[2]["namespace"], "miniagent")
        self.assertEqual(items[3], {"type": "function_call_output", "call_id": "call_1", "output": "file contents"})
        self.assertEqual(history, original)
        chat = ChatClient(Config(), "fake-key")
        self.assertNotIn("responses_reasoning", chat._payload(history, None, True)["messages"][0])

    def test_truncation_failure_and_partial_calls_never_escape(self):
        partial = {"type": "response.output_item.added", "output_index": 0, "item": call(status="in_progress")}
        for terminal in (None, {"type": "response.failed", "response": {"error": "synthetic-refresh"}},
                         {"type": "response.incomplete"}, completed(call(), status="incomplete")):
            wire = sse(partial, *([terminal] if terminal else []), done=False)
            with self.subTest(terminal=terminal), http_server([{"body": wire}]) as (url, requests):
                model = client(url)
                model.config.max_retries = 2
                with self.assertRaises(APIError) as caught:
                    model.complete([])
                self.assertNotIn("synthetic-refresh", str(caught.exception))
                self.assertEqual(len(requests), 1)

    def test_invalid_complete_outputs_rejected(self):
        cases = [(call(), call()), (call(namespace="other"),), (call(arguments=None),),
                 (call(status="in_progress"),), (message(), call()),
                 ({"type": "computer_call", "status": "completed"},),
                 (message("[commentary]\ntext", "final_answer"),)]
        for output in cases:
            with self.subTest(output=output), http_server([{"body": sse(completed(*output), done=False)}]) as (url, _):
                with self.assertRaises(APIError):
                    client(url).complete([])

    def test_completed_without_phase_does_not_end_agent_turn(self):
        with http_server([{"body": sse(completed(message("unmarked", None)), done=False)}]) as (url, _):
            result = client(url).complete([])
        self.assertNotIn("phase", result["choices"][0]["message"])

    def test_account_model_catalog(self):
        data = {"models": [{"slug": "visible-model", "visibility": "list"}, {"slug": "hidden-model", "visibility": "hidden"}]}
        with http_server([{"body": json.dumps(data).encode()}]) as (url, requests):
            self.assertEqual(client(url).list_models(), ["visible-model"])
        self.assertEqual(requests[0]["path"], "/v1/models")

    def test_errors_redact_all_credentials(self):
        body = json.dumps({"error": {"message": "synthetic-oauth synthetic-refresh"}}).encode()
        with http_server([{"status": 403, "body": body}]) as (url, _):
            with self.assertRaises(APIError) as caught:
                client(url).complete([])
        self.assertNotIn("synthetic-", str(caught.exception))

    def test_cli_read_tool_then_final_and_model_switch_keep_oauth(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "example.txt").write_text("read success", encoding="utf-8")
            responses = [{"body": sse(completed(call()), done=False)},
                         {"body": sse(completed(message("read success")), done=False)},
                         {"body": sse(completed(message("second model")), done=False)}]
            with http_server(responses) as (url, requests):
                output = io.StringIO()
                def make_client(config, auth):
                    result = client(url, auth)
                    result.config.model = config.model
                    return result
                with patch("miniagent.cli.ChatGPTAuth", return_value=Auth()), \
                     patch("miniagent.cli.ResponsesClient", side_effect=make_client), \
                     patch("miniagent.cli.load_api_key") as keys, \
                     patch("miniagent.config.user_directory", return_value=Path(directory) / "profile"), \
                     patch("sys.stdin", io.StringIO("read example\n/model another-model\nhello\n/exit\n")), \
                     redirect_stdout(output), redirect_stderr(output):
                    code = main(["--provider", "chatgpt", "--plain", "--no-save", "-C", directory])
            self.assertEqual(code, 0, output.getvalue())
            keys.assert_not_called()
            self.assertIn("read success", output.getvalue())
            self.assertEqual(requests[1]["body"]["input"][-1]["type"], "function_call_output")
            self.assertIn("read success", requests[1]["body"]["input"][-1]["output"])
            self.assertEqual(requests[2]["body"]["model"], "another-model")
            self.assertFalse((path / ".miniagent").exists())


if __name__ == "__main__":
    unittest.main()
