"""Fleet: a master console controlling worker consoles over their HTTP API."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from xts_agent.fleet import Fleet, WorkerError, partition, validate_workers

FP = "generic/aosp_cf/vsoc:17/AP1A/1:user/release-keys"


def _dev(serial, fp=FP, leased=""):
    return {"serial": serial, "state": "device", "booted": True, "leased_by": leased, "quarantine": None,
            "fingerprint": fp, "model": "cf", "abi": "x86_64"}


class PartitionTests(unittest.TestCase):
    def test_share_follows_device_count_and_is_complete(self):
        mods = [f"Cts{i}TestCases" for i in range(30)]
        parts = partition(mods, {"a": 4, "b": 2, "c": 0})
        self.assertEqual({k: len(v) for k, v in parts.items()}, {"a": 20, "b": 10})
        self.assertEqual(sorted(parts["a"] + parts["b"]), sorted(mods))  # nothing lost or duplicated
        self.assertEqual(partition(mods, {"a": 4, "b": 2, "c": 0}), parts)  # deterministic
        with self.assertRaises(ValueError):
            partition(mods, {"a": 0})
        self.assertEqual(list(partition(["one"], {"a": 1, "b": 1})), ["a"])  # no empty part

    def test_worker_list_validation_keeps_saved_token(self):
        saved = [{"name": "pc2", "url": "http://10.0.0.2:8765", "token": "old"}]
        out = validate_workers([{"name": "PC2", "url": "http://10.0.0.2:8765/", "token": ""},
                                {"name": "pc3", "url": "https://pc3.lab:8765", "token": "new"}], saved)
        self.assertEqual(out, [{"name": "pc2", "url": "http://10.0.0.2:8765", "token": "old"},
                               {"name": "pc3", "url": "https://pc3.lab:8765", "token": "new"}])
        for bad in ([{"name": "a b", "url": "http://h:1"}], [{"name": "a", "url": "ftp://h"}],
                    [{"name": "a", "url": "http://h:1/path"}], [{"name": "a", "url": "http://h:1"}] * 2,
                    [{"name": "a", "url": "http://h:1", "token": "x y"}]):
            with self.assertRaises(ValueError):
                validate_workers(bad)


class FleetEndToEndTests(unittest.TestCase):
    """Two real worker consoles on loopback, one master app."""

    def _console(self, root: Path, token: str = "", serve: bool = False):
        from xts_agent.ui.server import UiApp, UiConfig, make_server

        (root / "config" / "test_plans").mkdir(parents=True)
        (root / "config" / "default_config.yaml").write_text("{}\n", encoding="utf-8")
        cfg = UiConfig(root=root, results_dir=root / "results", plans_dir=root / "config" / "test_plans",
                       db_path=None, lease_dir=str(root / "leases"),
                       defaults_path=str(root / "config" / "default_config.yaml"), token=token, port=0)
        if not serve:
            return UiApp(cfg), None
        server = make_server(cfg)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.RequestHandlerClass.app, f"http://127.0.0.1:{server.server_address[1]}"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("XTS_LOCAL_SETTINGS", None)
        root = Path(tmp.name)
        self.w1, url1 = self._console(root / "pc1", token="t1", serve=True)
        self.w2, url2 = self._console(root / "pc2", token="t2", serve=True)
        self.w1.devices.list = lambda refresh=False: [_dev("A1"), _dev("A2"), _dev("A3"), _dev("A4")]
        self.w2.devices.list = lambda refresh=False: [_dev("B1"), _dev("B2"), _dev("B3", leased="pid=1 user=u job=-")]
        self.master, _ = self._console(root / "master")
        self.plan = self.master.cfg.plans_dir / "nightly.yaml"
        mods = [f"Cts{i:02d}TestCases" for i in range(12)]
        self.plan.write_text(yaml.safe_dump({"name": "Nightly", "suites": [
            {"name": "cts", "plan": "cts", "include_filters": mods}]}), encoding="utf-8")
        self.master.save_settings({"fleet": {"workers": [
            {"name": "pc1", "url": url1, "token": "t1"}, {"name": "pc2", "url": url2, "token": "t2"}]}})
        self.fleet: Fleet = self.master.fleet

    def test_settings_never_return_tokens(self):
        st = self.master.settings()
        self.assertEqual([(w["name"], w["has_token"]) for w in st["fleet"]["workers"]], [("pc1", True), ("pc2", True)])
        self.assertNotIn("t1", json.dumps(st))
        self.assertEqual(oct(self.master.cfg.settings_file.stat().st_mode & 0o777), "0o600")
        # re-saving without tokens keeps them; an empty list removes the section
        self.master.save_settings({"fleet": {"workers": [{"name": "pc1", "url": self.fleet.worker("pc1").url}]}})
        self.assertEqual(self.fleet.worker("pc1").token, "t1")
        self.master.save_settings({"fleet": {"workers": []}})
        self.assertEqual(self.fleet.workers(), [])

    def test_status_shows_every_workers_devices(self):
        rows = {r["name"]: r for r in self.fleet.status(refresh=True)}
        self.assertTrue(rows["pc1"]["online"] and rows["pc2"]["online"])
        self.assertEqual((rows["pc1"]["counts"]["free"], rows["pc2"]["counts"]["free"]), (4, 2))
        self.assertEqual({d["worker"] for d in rows["pc2"]["devices"]}, {"pc2"})
        self.assertEqual(rows["pc1"]["fingerprints"], [FP])

    def test_offline_or_wrong_token_worker_is_reported_not_fatal(self):
        self.master.save_settings({"fleet": {"workers": [
            {"name": "pc1", "url": self.fleet.worker("pc1").url, "token": "wrong"},
            {"name": "gone", "url": "http://127.0.0.1:9"}]}})
        rows = {r["name"]: r for r in self.fleet.status(refresh=True)}
        self.assertFalse(rows["pc1"]["online"])
        self.assertIn("token", rows["pc1"]["error"])
        self.assertFalse(rows["gone"]["online"])
        self.assertIn("unreachable", rows["gone"]["error"])

    def test_proxy_is_allow_listed(self):
        self.assertEqual(len(self.fleet.proxy("pc1", "GET", "/api/devices")), 4)
        for method, path in (("GET", "/api/settings"), ("POST", "/api/settings"), ("POST", "/api/plans/delete"),
                             ("GET", "/files/x.html"), ("GET", "/api/../api/settings"), ("POST", "/api/devices")):
            with self.assertRaises(LookupError):
                self.fleet.proxy("pc1", method, path, body={})
        with self.assertRaises(LookupError):
            self.fleet.proxy("nobody", "GET", "/api/devices")

    def test_dispatch_pushes_the_plan_and_launches_on_the_worker(self):
        with patch("xts_agent.ui.server.subprocess.Popen") as popen:
            popen.return_value = MagicMock(pid=4321)
            out = self.fleet.dispatch({"worker": "pc2", "plan": "nightly.yaml", "dry_run": True,
                                       "serials": ["B1"], "shard_count": 1})
        self.assertEqual((out["worker"], out["plan"]), ("pc2", "fleet-nightly.yaml"))
        self.assertTrue(out["run_id"].startswith("ui-"))
        pushed = self.w2.cfg.plans_dir / "fleet-nightly.yaml"
        self.assertTrue(pushed.exists())
        self.assertFalse((self.w1.cfg.plans_dir / "fleet-nightly.yaml").exists())
        cmd = popen.call_args.args[0]
        self.assertIn("--dry-run", cmd)
        derived = yaml.safe_load(Path(cmd[cmd.index("--plan") + 1]).read_text(encoding="utf-8"))
        self.assertEqual(derived["devices"]["serials"], ["B1"])
        with self.assertRaises(ValueError):
            self.fleet.dispatch({"worker": "pc2", "plan": "../../etc/passwd"})

    def test_split_preview_then_start(self):
        pre = self.fleet.split({"plan": "nightly.yaml"}, preview=True)
        self.assertEqual({p["worker"]: p["modules"] for p in pre["parts"]}, {"pc1": 8, "pc2": 4})  # 4 vs 2 free
        self.assertEqual(pre["total_modules"], 12)
        self.assertFalse(self.fleet.runs())  # a preview starts nothing
        with patch("xts_agent.ui.server.subprocess.Popen") as popen:
            popen.return_value = MagicMock(pid=1)
            run = self.fleet.split({"plan": "nightly.yaml", "dry_run": True})
        self.assertEqual(popen.call_count, 2)
        sent = []
        for call in popen.call_args_list:
            cmd = call.args[0]
            plan = yaml.safe_load(Path(cmd[cmd.index("--plan") + 1]).read_text(encoding="utf-8"))
            sent += plan["suites"][0]["include_filters"]
        self.assertEqual(sorted(sent), [f"Cts{i:02d}TestCases" for i in range(12)])  # every module exactly once
        self.assertEqual([p["modules"] for p in run["parts"]], [8, 4])
        self.assertTrue(all(p["run_id"] for p in run["parts"]))
        self.assertEqual(len(self.fleet.runs()), 1)

    def test_split_refuses_mixed_builds_and_busy_workers(self):
        self.w2.devices.list = lambda refresh=False: [_dev("B1", fp="other/build:17/X/2:user/release-keys")]
        with self.assertRaisesRegex(RuntimeError, "one build"):
            self.fleet.split({"plan": "nightly.yaml"}, preview=True)
        self.w2.devices.list = lambda refresh=False: [_dev("B1", leased="pid=1 user=u job=-")]
        with self.assertRaisesRegex(RuntimeError, "no free device"):
            self.fleet.split({"plan": "nightly.yaml"}, preview=True)
        pre = self.fleet.split({"plan": "nightly.yaml", "workers": ["pc1"], "serials": {"pc1": ["A1", "A2"]}}, preview=True)
        self.assertEqual(pre["parts"][0]["devices"], ["A1", "A2"])
        with self.assertRaisesRegex(RuntimeError, "not free"):
            self.fleet.split({"plan": "nightly.yaml", "workers": ["pc1"], "serials": {"pc1": ["ZZ"]}}, preview=True)

    def test_merged_fleet_run_and_stop(self):
        with patch("xts_agent.ui.server.subprocess.Popen") as popen:
            popen.return_value = MagicMock(pid=1)
            run = self.fleet.split({"plan": "nightly.yaml"})
        self.assertEqual(run["state"], "unknown")  # nothing reported by the (mocked) agents yet
        ids = [p["run_id"] for p in run["parts"]]
        reported = {
            "pc1": [{"run_id": ids[0], "key": "nightly", "state": "running", "phase": "running",
                     "totals": {"pass": 40, "fail": 2, "skip": 1}}],
            "pc2": [{"run_id": ids[1], "key": ids[1], "state": "done", "status": "PASSED", "pass": 25, "fail": 0}],
        }
        with patch.object(Fleet, "_worker_runs", side_effect=lambda name: reported[name]):
            live = self.fleet.run(run["id"])
            self.assertEqual((live["state"], live["totals"]), ("running", {"pass": 65, "fail": 2, "skip": 1}))
            with patch("xts_agent.fleet.Worker.post") as post:
                self.fleet.stop(run["id"])
            self.assertEqual([c.args[0] for c in post.call_args_list], [f"/api/runs/{ids[0]}/stop"])  # only the running part
            reported["pc1"] = [{"run_id": ids[0], "state": "done", "status": "FAILED", "pass": 90, "fail": 3}]
            done = self.fleet.run(run["id"])
        self.assertEqual((done["state"], done["status"], done["totals"]["pass"]), ("done", "FAILED", 115))
        # finished parts are stored, so the outcome survives a worker going away
        with patch.object(Fleet, "_worker_runs", return_value=[]):
            self.assertEqual(self.fleet.run(run["id"])["status"], "FAILED")

    def test_http_routes(self):
        import urllib.error
        import urllib.request

        _, url = self._console(Path(self.master.cfg.root).parent / "m2", serve=True)
        m2 = Path(self.master.cfg.root).parent / "m2"
        (m2 / "config" / "local_settings.yaml").write_text(self.master.cfg.settings_file.read_text(), encoding="utf-8")

        def get(path, method="GET", body=None):
            req = urllib.request.Request(url + path, method=method, headers={"X-XTS-UI": "1"},
                                         data=json.dumps(body).encode() if body is not None else None)
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        code, data = get("/api/fleet")
        self.assertEqual((code, len(data["workers"])), (200, 2))
        self.assertEqual(get("/api/fleet/w/pc1/api/devices")[0], 200)
        self.assertEqual(get("/api/fleet/w/pc1/api/settings")[0], 404)
        self.assertEqual(get("/api/fleet/w/nope/api/devices")[0], 404)
        self.assertEqual(get("/api/fleet/runs")[1], [])
        code, data = get("/api/fleet/split", "POST", {"plan": "missing.yaml", "preview": True})
        self.assertEqual(code, 400)
        self.assertIsInstance(WorkerError("x"), RuntimeError)


if __name__ == "__main__":
    unittest.main()
