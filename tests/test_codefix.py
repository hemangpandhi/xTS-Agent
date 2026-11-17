"""Code-fix stage: patch validation, build/flash flow, revert, and the loop wiring."""

from __future__ import annotations

import json
import subprocess
import threading

import pytest

from xts_agent.fixloop.codefix import CodeFixConfig, CodeFixer, Edit, Patch, PatchError
from xts_agent.fixloop.executor import TestTarget
from xts_agent.fixloop.loop import FIXED_IN_CODE, NEEDS_CODE_FIX, FixLoop
from xts_agent.fixloop.oem_sources import OemCodeLocator, OemSourceConfig
from xts_agent.fixloop.planner import Planner

from .test_fixloop import FakeDevice, ScriptedExecutor, catalog, fail, outcome  # noqa: F401

STRINGS = '<resources>\n    <string name="camera_recently_accessed">Recent camera activity</string>\n</resources>\n'


@pytest.fixture
def oem_tree(tmp_path):
    repo = tmp_path / "Settings"
    (repo / "res/values").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "res/values/strings.xml").write_text(STRINGS)
    (repo / "tests/FooTest.java").write_text("class FooTest {}\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
                   check=True)
    return repo


def src(repo, editable=True):
    return OemSourceConfig(name="CarSettings", path=str(repo), editable=editable, build_targets=["CarSettings"],
                           packages=["com.android.car.settings"])


class PatchLLM:
    model = "fake-coder"

    def __init__(self, patches):
        self.patches = list(patches)
        self.prompts = []

    def generate(self, prompt, json_mode=False):
        self.prompts.append(prompt)
        if "List up to 4 identifiers" in prompt:
            return json.dumps({"terms": ["camera_recently_accessed"]})
        if "smallest source change" in prompt:
            return json.dumps(self.patches.pop(0))
        return json.dumps({"classification": "PRODUCT_BUG", "confidence": 0.8, "root_cause": "string renamed",
                           "remediation_ids": []})


def fixer(repo, provider=None, runner=None, tmp_path=None, **cfg):
    c = CodeFixConfig(enabled=True, build_command="build {modules}", deploy_command="flash {serial}", **cfg)
    return CodeFixer(c, [src(repo)], provider or PatchLLM([]), threading.Lock(), tmp_path or repo.parent,
                     runner=runner or (lambda cmd, cwd, t, log: (0, "ok")))


GOOD = {"root_cause": "OEM renamed the CDD string", "edits": [{
    "file": "", "find": '<string name="camera_recently_accessed">Recent camera activity</string>',
    "replace": '<string name="camera_recently_accessed">Recently accessed</string>'}]}


def good(repo):
    g = json.loads(json.dumps(GOOD))
    g["edits"][0]["file"] = str(repo / "res/values/strings.xml")
    return g


def test_patch_applies_diffs_and_reverts(oem_tree):
    f = fixer(oem_tree)
    path = str(oem_tree / "res/values/strings.xml")
    p = Patch(edits=[Edit(path, GOOD["edits"][0]["find"], GOOD["edits"][0]["replace"])])
    f.apply(p, [path])
    assert "Recently accessed" in (oem_tree / "res/values/strings.xml").read_text()
    assert "+    <string name=\"camera_recently_accessed\">Recently accessed" in p.diff
    assert p.modules == ["CarSettings"]
    f.revert()
    assert (oem_tree / "res/values/strings.xml").read_text() == STRINGS


@pytest.mark.parametrize("edit, why", [
    (lambda r: Edit(str(r / "tests/FooTest.java"), "class FooTest {}", "class FooTest { }"), "never edited"),
    (lambda r: Edit(str(r / "res/values/strings.xml"), "<nope/>", "x"), "occurs 0 times"),
    (lambda r: Edit("/etc/passwd", "root", "x"), "outside every editable"),
])
def test_bad_edits_rejected_and_nothing_written(oem_tree, edit, why):
    f = fixer(oem_tree)
    e = edit(oem_tree)
    with pytest.raises(PatchError, match=why):
        f.apply(Patch(edits=[e]), [e.file])
    assert (oem_tree / "res/values/strings.xml").read_text() == STRINGS


def test_read_only_source_is_not_editable(oem_tree):
    c = CodeFixConfig(enabled=True, build_command="b", deploy_command="d")
    f = CodeFixer(c, [src(oem_tree, editable=False)], PatchLLM([]), threading.Lock(), oem_tree.parent)
    assert not f.available


def make(oem_tree, tmp_path, outcomes, patches, runner=None):
    llm = PatchLLM(patches)
    locator = OemCodeLocator([src(oem_tree)])
    dev = FakeDevice({})
    ex = ScriptedExecutor(outcomes)
    from xts_agent.fixloop.catalog import RemediationCatalog

    cat_path = tmp_path / "empty.yaml"
    cat_path.write_text("remediations: []\n")
    loop = FixLoop(ex, dev, Planner(RemediationCatalog.load(cat_path), locator, llm), out_dir=tmp_path / "out",
                   flaky_retries=0, code_fixer=fixer(oem_tree, llm, runner, tmp_path / "out"))
    return loop, llm


RED = outcome(failed=1, failures=[fail("Could not find camera_recently_accessed 'Recently accessed'")])


def test_product_bug_fixed_in_code(oem_tree, tmp_path):
    calls = []
    loop, _ = make(oem_tree, tmp_path, [RED, outcome(passed=2), outcome(passed=2)], [good(oem_tree)],
                   runner=lambda cmd, cwd, t, log: (calls.append(cmd), (0, "ok"))[1])
    s = loop.run(TestTarget("CtsSettingsTestCases"))
    assert s.status == FIXED_IN_CODE
    assert calls == ["build CarSettings", "flash emu-1"]
    assert "Recently accessed" in (oem_tree / "res/values/strings.xml").read_text()  # kept for review
    assert open(s.patch_file).read().count("+    <string") == 1
    kinds = [i.kind for i in s.iterations]
    assert kinds == ["baseline", "code_fix", "verify_fix", "confirm"]


def test_build_failure_feeds_back_and_unfixed_patch_is_reverted(oem_tree, tmp_path):
    results = iter([(1, "error: resource bad"), (0, "ok"), (0, "ok"), (0, "ok"), (0, "ok")])
    loop, llm = make(oem_tree, tmp_path, [RED, RED, RED], [good(oem_tree), good(oem_tree)],
                     runner=lambda cmd, cwd, t, log: next(results))
    s = loop.run(TestTarget("CtsSettingsTestCases"))
    assert s.status == NEEDS_CODE_FIX
    patch_prompts = [p for p in llm.prompts if "smallest source change" in p]
    assert "did not build" in patch_prompts[-1] or "still fails" in patch_prompts[-1]
    verdicts = [i.verdict for i in s.iterations if i.kind in ("code_fix", "restore")]
    assert verdicts[0] == "build_failed" and verdicts[-1] == "restored"
    assert (oem_tree / "res/values/strings.xml").read_text() == STRINGS


def test_find_block_with_wrong_indentation_still_applies_once(oem_tree):
    f = fixer(oem_tree)
    path = str(oem_tree / "res/values/strings.xml")
    e = Edit(path, '      <string name="camera_recently_accessed">Recent camera activity</string>',
             '      <string name="camera_recently_accessed">Recently accessed</string>')
    f.apply(Patch(edits=[e]), [path])
    assert '\n    <string name="camera_recently_accessed">Recently accessed</string>\n' in \
        (oem_tree / "res/values/strings.xml").read_text()


def test_reflowed_find_block_matches_whole_lines(tmp_path):
    from xts_agent.fixloop.codefix import _replace_by_lines

    text = "int alpha = compute(\n        beta, gamma);\nint d;\n"
    out = _replace_by_lines(text, "int alpha = compute( beta, gamma);", "int alpha = compute(beta, gamma, e);")
    assert out == "int alpha = compute(beta, gamma, e);\nint d;\n"
    assert _replace_by_lines(text, "int alpha = compute( beta, delta);", "y") is None


class StubbornReviewLLM(PatchLLM):
    """Always rejects in review; the same patch proposed twice must still reach the DUT."""

    def generate(self, prompt, json_mode=False):
        if "reviewing a proposed OEM source patch" in prompt:
            self.prompts.append(prompt)
            return json.dumps({"ok": False, "problems": ["I do not believe it"]})
        if "smallest source change" in prompt:
            self.prompts.append(prompt)
            return json.dumps(self.patches[0])
        return super().generate(prompt, json_mode)


def test_review_veto_is_overruled_when_the_same_patch_is_proposed_again(oem_tree, tmp_path):
    loop, _ = make(oem_tree, tmp_path, [RED, outcome(passed=2), outcome(passed=2)], [good(oem_tree)])
    loop.planner.provider = loop.code_fixer.provider = llm = StubbornReviewLLM([good(oem_tree)])
    s = loop.run(TestTarget("CtsSettingsTestCases"))
    assert s.status == FIXED_IN_CODE
    verdicts = [i.verdict for i in s.iterations if i.kind == "code_fix"]
    assert verdicts == ["patch_rejected", "flashed"]
    assert sum("reviewing a proposed" in p for p in llm.prompts) == 1
