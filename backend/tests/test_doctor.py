"""The first-run doctor must diagnose prerequisites, not crash on them.

A user who skipped MongoDB, Ollama or a model pull should get a clear fix,
and the exit code must reflect whether anything required is actually missing.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# scripts/ is not a package; load the module by path.
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "doctor.py"


@pytest.fixture(scope="module")
def doctor():
    spec = importlib.util.spec_from_file_location("circle_doctor", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["circle_doctor"] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class TestModelMatching:
    def test_exact_name_matches(self, doctor):
        assert doctor._has_model(["gemma3:4b"], "gemma3:4b") is True

    def test_tag_is_ignored(self, doctor):
        """`ollama pull gemma3:4b` can list as `gemma3:4b` or `gemma3:latest`;
        the family name is what matters."""
        assert doctor._has_model(["gemma3:latest"], "gemma3:4b") is True
        assert doctor._has_model(["nomic-embed-text:latest"],
                                 "nomic-embed-text") is True

    def test_different_model_does_not_match(self, doctor):
        assert doctor._has_model(["llama3:latest"], "gemma3:4b") is False

    def test_empty_list_is_missing(self, doctor):
        assert doctor._has_model([], "gemma3:4b") is False


class TestChecks:
    def test_missing_model_reports_the_pull_command(self, doctor):
        c = doctor.check_model(["llama3:latest"], "gemma3:4b", "Reasoning model")
        assert c.status == doctor.FAIL
        assert "ollama pull gemma3:4b" in c.fix

    def test_present_model_is_ok(self, doctor):
        c = doctor.check_model(["gemma3:4b"], "gemma3:4b", "Reasoning model")
        assert c.status == doctor.OK

    def test_unreachable_ollama_says_how_to_start_it(self, doctor, monkeypatch):
        monkeypatch.setattr(doctor, "_ollama_tags",
                            lambda base: ([], "connection refused"))
        c, names = doctor.check_ollama("http://localhost:11434")
        assert c.status == doctor.FAIL
        assert names == []
        assert "ollama.com/download" in c.fix

    def test_unreachable_mongodb_gives_a_docker_command(self, doctor, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "mongodb://127.0.0.1:1")
        c = doctor.check_mongodb("mongodb://127.0.0.1:1")
        assert c.status == doctor.FAIL
        assert "docker run" in c.fix

    def test_missing_node_is_a_warning_not_a_failure(self, doctor, monkeypatch):
        """The UI can still be served from an existing dist, so this must not
        block startup."""
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        c = doctor.check_node()
        assert c.status == doctor.WARN
        assert c.failed is False

    def test_python_too_old_fails(self, doctor, monkeypatch):
        monkeypatch.setattr(doctor.sys, "version_info", (3, 9, 0))
        c = doctor.check_python()
        assert c.status == doctor.FAIL


class TestReport:
    def test_ok_when_only_warnings(self, doctor):
        report = doctor.Report(checks=[
            doctor.Check("MongoDB", doctor.OK, "fine"),
            doctor.Check("Node", doctor.WARN, "no npm"),
        ])
        assert report.ok is True

    def test_not_ok_when_something_failed(self, doctor):
        report = doctor.Report(checks=[
            doctor.Check("MongoDB", doctor.FAIL, "down"),
            doctor.Check("Node", doctor.WARN, "no npm"),
        ])
        assert report.ok is False
        assert [c.name for c in report.failures] == ["MongoDB"]

    def test_render_lists_fixes_for_failures(self, doctor):
        report = doctor.Report(checks=[
            doctor.Check("MongoDB", doctor.FAIL, "cannot reach it",
                         "docker run -d -p 27017:27017 mongo:7"),
            doctor.Check("Ollama", doctor.OK, "reachable"),
        ])
        text = doctor.render(report)
        assert "[fail]" in text
        assert "docker run" in text
        assert "Fix these" in text

    def test_render_says_ready_when_ok(self, doctor):
        report = doctor.Report(checks=[doctor.Check("MongoDB", doctor.OK, "fine")])
        text = doctor.render(report)
        assert "ready" in text.lower()

    def test_render_has_no_emoji(self, doctor):
        """Windows consoles raise UnicodeEncodeError on emoji; the report must
        stay plain ASCII."""
        report = doctor.Report(checks=[
            doctor.Check("MongoDB", doctor.FAIL, "down", "start it"),
            doctor.Check("Node", doctor.WARN, "no npm"),
        ])
        doctor.render(report).encode("ascii")  # raises if anything is not ASCII

    def test_json_output_is_valid_and_machine_readable(self, doctor, monkeypatch,
                                                       capsys):
        monkeypatch.setattr(doctor, "run_checks", lambda: doctor.Report(
            checks=[doctor.Check("MongoDB", doctor.FAIL, "down", "fix it")]))
        rc = doctor.main(["doctor.py", "--json"])
        import json
        out = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert out["ok"] is False
        assert out["checks"][0]["name"] == "MongoDB"

    def test_exit_code_is_zero_when_all_ok(self, doctor, monkeypatch):
        monkeypatch.setattr(doctor, "run_checks", lambda: doctor.Report(
            checks=[doctor.Check("MongoDB", doctor.OK, "fine")]))
        assert doctor.main(["doctor.py"]) == 0


class TestOllamaProbe:
    def test_successful_probe_returns_names(self, doctor, monkeypatch):
        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"models":[{"name":"gemma3:4b"}]}'

        monkeypatch.setattr(doctor.urllib.request, "urlopen",
                            lambda url, timeout: FakeResp())
        names, err = doctor._ollama_tags("http://localhost:11434")
        assert names == ["gemma3:4b"]
        assert err == ""
