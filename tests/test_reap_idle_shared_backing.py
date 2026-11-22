"""Testes de lógica pura do FinOps idle reaper (`scripts/reap_idle_shared_backing.py`).

Nenhuma chamada real a gcloud/gh/git: um runner falso simula o projeto `ernani-demo-argolis`, a API do GitHub
e o `origin/main` do repositório, registrando cada comando — assim provamos, por exemplo, que o dry-run
nunca executa comandos de alteração e que a reavaliação completa acontece antes de cada alteração.

Rodar: `python3 -m pytest tests/ -q` ou `python3 -m unittest discover -s tests -v`.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "reap_idle_shared_backing.py"
_SPEC = importlib.util.spec_from_file_location("reap_idle_shared_backing", SCRIPT)
reaper = importlib.util.module_from_spec(_SPEC)
sys.modules["reap_idle_shared_backing"] = reaper
_SPEC.loader.exec_module(reaper)

NOW = 1795160000  # 2026-11-20T07:33:20Z
REPO = "gemini-enterprise-sdlc-org/cloud-provisioning-approvals"
PGSQL = "sdlc-shared-pgsql"
CONN = f"ernani-demo-argolis:southamerica-east1:{PGSQL}"
PGSQL_IP = "35.247.213.73"


def pgsql_instance(**overrides):
    inst = {
        "name": PGSQL,
        "state": "RUNNABLE",
        "connectionName": CONN,
        "region": "southamerica-east1",
        "databaseVersion": "POSTGRES_16",
        "ipAddresses": [
            {"type": "PRIMARY", "ipAddress": PGSQL_IP},
            {"type": "OUTGOING", "ipAddress": "35.247.235.174"},
        ],
        "settings": {
            "activationPolicy": "ALWAYS",
            "tier": "db-custom-2-7680",
            "deletionProtectionEnabled": False,
            "userLabels": {
                "managed_by": "cloud-provisioning-approvals",
                "shared_singleton": "postgresql",
                "finops_allocation": "proportional-per-db-stats",
            },
        },
    }
    for key, value in overrides.items():
        if key == "settings":
            inst["settings"].update(value)
        else:
            inst[key] = value
    return inst


def redis_instance(**overrides):
    inst = {
        "name": "projects/ernani-demo-argolis/locations/southamerica-east1/instances/sdlc-shared-redis",
        "state": "READY",
        "host": "10.20.30.40",
        "tier": "BASIC",
        "memorySizeGb": 2,
        "labels": {"managed_by": "cloud-provisioning-approvals", "shared_singleton": "redis"},
    }
    inst.update(overrides)
    return inst


def lease(slug, status="DECOMMISSIONED", decommissioned="2026-11-20T04:36:57Z", expires=1795149137, **extra):
    data = {
        "workload_slug": slug,
        "status": status,
        "target_gcp_project": "ernani-demo-argolis",
        "region": "southamerica-east1",
        "database_type": "postgresql",
        "shared_sql_instance": PGSQL,
        "shared_redis_instance": "",
        "expires_at_epoch": expires,
    }
    if decommissioned:
        data["decommissioned_at_utc"] = decommissioned
    data.update(extra)
    return data


def active_lease(slug, **extra):
    return lease(slug, status="ACTIVE", decommissioned=None, expires=NOW + 3600, **extra)


def run_service(name, annotation=None, env=None):
    template_meta = {"annotations": {}}
    if annotation:
        template_meta["annotations"]["run.googleapis.com/cloudsql-instances"] = annotation
    return {
        "metadata": {"name": name},
        "spec": {"template": {"metadata": template_meta, "spec": {"containers": [{"env": env or []}]}}},
    }


class FakeWorld:
    """Estado simulado do GCP + GitHub + origin/main, alterado pelos comandos de mutação do runner falso."""

    def __init__(self):
        self.sql = {PGSQL: pgsql_instance()}
        self.databases = {PGSQL: ["postgres"]}
        self.operations = {PGSQL: []}
        self.redis = None
        self.services = {
            "southamerica-east1": [
                run_service("ponto-api-dev", annotation="ernani-demo-argolis:southamerica-east1:ponto-eletronico"),
                run_service("google-maps-portal"),
            ],
            "us-central1": [run_service("sdlc-factory")],
        }
        self.jobs = {"southamerica-east1": [], "us-central1": []}
        self.in_flight = {}
        self.gh_fail = False
        self.remote_leases = {"todo-mobile.json": json.dumps(lease("todo-mobile"))}
        self.git_fetch_fail = False
        self.fail_patch = False
        self.describe_error = None  # stderr simulado para describe do Cloud SQL
        self.gclb = {}  # chave de reaper.GCLB_NAMES (+ fallback_bucket) -> recurso; ausente = não existe
        self.cert_entries = {}  # nome -> entrada do Certificate Map
        self.gclb_fail = {}  # chave -> nº de falhas restantes no delete
        self.calls = []
        self.on_call = None  # callback(world, argv) para simular corridas/estado assíncrono

    # ---- runner ----------------------------------------------------------------------------
    def __call__(self, argv):
        self.calls.append(list(argv))
        if self.on_call:
            self.on_call(self, list(argv))
        if argv[0] == "gh":
            return self._gh(argv)
        if argv[0] == "git":
            return self._git(argv)
        return self._gcloud(argv)

    @staticmethod
    def ok(payload):
        return reaper.CmdResult(0, json.dumps(payload), "")

    def _gcloud(self, argv):
        args = [a for a in argv[1:] if not a.startswith("--project=") and a != "--format=json"]
        flags = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in args if a.startswith("--") and "=" in a}
        pos = [a for a in args if not a.startswith("--")]
        if pos[:3] == ["sql", "instances", "describe"]:
            if self.describe_error:
                return reaper.CmdResult(1, "", self.describe_error)
            name = pos[3]
            if name in self.sql:
                return self.ok(self._presented(self.sql[name]))
            return reaper.CmdResult(1, "", "ERROR: (gcloud.sql.instances.describe) HTTPError 404: The Cloud SQL instance does not exist.")
        if pos[:3] == ["sql", "databases", "list"]:
            return self.ok([{"name": d} for d in self.databases.get(flags["--instance"], [])])
        if pos[:3] == ["sql", "operations", "list"]:
            return self.ok(self.operations.get(flags["--instance"], []))
        if pos[:3] == ["sql", "instances", "patch"]:
            if self.fail_patch:
                return reaper.CmdResult(1, "", "ERROR: (gcloud.sql.instances.patch) HTTPError 409: operation in progress")
            name, policy = pos[3], flags["--activation-policy"]
            self.sql[name]["settings"]["activationPolicy"] = policy
            return self.ok({"name": f"op-{policy.lower()}-1", "operationType": "UPDATE", "status": "PENDING"})
        if pos[:3] == ["sql", "operations", "wait"]:
            return self.ok({"name": pos[3], "status": "DONE"})
        if pos[:3] == ["redis", "instances", "describe"]:
            if self.redis is not None:
                return self.ok(self.redis)
            return reaper.CmdResult(1, "", "ERROR: (gcloud.redis.instances.describe) NOT_FOUND: Resource 'x' was not found.")
        if pos[:3] == ["redis", "instances", "delete"]:
            self.redis = None
            return self.ok({"name": "projects/ernani-demo-argolis/locations/southamerica-east1/operations/operation-r1"})
        if pos[:3] == ["run", "services", "list"]:
            return self.ok(self.services.get(flags["--region"], []))
        if pos[:3] == ["run", "jobs", "list"]:
            return self.ok(self.jobs.get(flags["--region"], []))
        gclb = self._gclb(pos, flags)
        if gclb is not None:
            return gclb
        raise AssertionError(f"comando gcloud inesperado: {argv}")

    # ---- GCLB compartilhado ------------------------------------------------------------------
    GCLB_DEPENDENTS = {
        # recurso -> quem precisa ter sido removido antes (ordem reversa de dependência)
        "https_proxy": ["https_fwd"],
        "http_proxy": ["http_fwd"],
        "https_urlmap": ["https_proxy"],
        "http_redirect": ["http_proxy"],
        "fallback_backend": ["https_urlmap"],
        "fallback_bucket": ["fallback_backend"],
        "ip": ["https_fwd", "http_fwd"],
        "cert_map": ["https_proxy"],
    }

    def _gclb_key(self, kind, name):
        names = {v: k for k, v in reaper.GCLB_NAMES.items()}
        if kind == "storage":
            return "fallback_bucket" if name == f"gs://{reaper.GCLB_DEFAULT_FALLBACK_BUCKET}" else None
        return names.get(name)

    def _gclb(self, pos, flags):
        not_found = reaper.CmdResult(1, "", "ERROR: Could not fetch resource:\n - The resource 'projects/ernani-demo-argolis/global/x/y' was not found")
        cm_not_found = reaper.CmdResult(1, "", "ERROR: NOT_FOUND: Resource 'projects/ernani-demo-argolis/locations/global/x' was not found.")
        if pos[:3] == ["certificate-manager", "maps", "entries"]:
            if "cert_map" not in self.gclb:
                return cm_not_found
            if pos[3] == "list":
                return self.ok(list(self.cert_entries.values()))
            if pos[3] == "delete":
                if pos[4] not in self.cert_entries:
                    return cm_not_found
                del self.cert_entries[pos[4]]
                return self.ok({})
        if pos[:2] == ["storage", "buckets"] and pos[2] == "describe":
            return self.ok(self.gclb["fallback_bucket"]) if self._gclb_key("storage", pos[3]) in self.gclb else reaper.CmdResult(1, "", f"ERROR: {pos[3]} not found: 404.")
        if pos[:2] == ["storage", "rm"]:
            key = self._gclb_key("storage", pos[2])
            return self._gclb_delete(key, reaper.CmdResult(1, "", "ERROR: The following URLs matched no objects or files"))
        kinds = {
            ("compute", "forwarding-rules"), ("compute", "target-https-proxies"), ("compute", "target-http-proxies"),
            ("compute", "url-maps"), ("compute", "backend-buckets"), ("compute", "addresses"),
            ("certificate-manager", "maps"), ("certificate-manager", "certificates"),
        }
        if tuple(pos[:2]) in kinds and pos[2] in {"describe", "delete"}:
            key = self._gclb_key("other", pos[3])
            miss = cm_not_found if pos[0] == "certificate-manager" else not_found
            if pos[2] == "describe":
                return self.ok(self.gclb[key]) if key in self.gclb else miss
            return self._gclb_delete(key, miss)
        return None

    def _gclb_delete(self, key, miss):
        if key not in self.gclb:
            return miss
        if self.gclb_fail.get(key, 0) > 0:
            self.gclb_fail[key] -= 1
            return reaper.CmdResult(1, "", "ERROR: HTTPError 503: backendError")
        blockers = [d for d in self.GCLB_DEPENDENTS.get(key, []) if d in self.gclb]
        if key == "cert_map" and self.cert_entries:
            blockers.append("entradas do Certificate Map")
        if key == "bootstrap_cert" and self.cert_entries:
            blockers.append("entrada primária")
        if blockers:
            return reaper.CmdResult(1, "", f"ERROR: resourceInUseByAnotherResource: {key} em uso por {blockers}")
        del self.gclb[key]
        return self.ok({})

    def with_gclb(self, host_rules=0):
        ip = "136.68.168.106"
        fwd = "https://www.googleapis.com/compute/v1/projects/ernani-demo-argolis/global/forwardingRules/"
        n = reaper.GCLB_NAMES
        self.gclb = {
            "https_fwd": {"name": n["https_fwd"], "IPAddress": ip, "labels": {"managed_by": "cloud-provisioning-approvals"}},
            "http_fwd": {"name": n["http_fwd"], "IPAddress": ip},
            "https_proxy": {"name": n["https_proxy"]},
            "http_proxy": {"name": n["http_proxy"]},
            "https_urlmap": {"name": n["https_urlmap"], "hostRules": [{"hosts": [f"app-{i}-frontend.{ip}.nip.io"], "pathMatcher": f"pm-{i}"} for i in range(host_rules)], "pathMatchers": []},
            "http_redirect": {"name": n["http_redirect"]},
            "fallback_backend": {"name": n["fallback_backend"], "bucketName": reaper.GCLB_DEFAULT_FALLBACK_BUCKET},
            "fallback_bucket": {"name": reaper.GCLB_DEFAULT_FALLBACK_BUCKET, "labels": {"managed_by": "cloud-provisioning-approvals"}},
            "ip": {"name": n["ip"], "address": ip, "labels": {"managed_by": "cloud-provisioning-approvals"}, "users": [fwd + n["https_fwd"], fwd + n["http_fwd"]]},
            "cert_map": {"name": f"projects/ernani-demo-argolis/locations/global/certificateMaps/{n['cert_map']}"},
            "bootstrap_cert": {"name": f"projects/ernani-demo-argolis/locations/global/certificates/{n['bootstrap_cert']}"},
        }
        self.cert_entries = {
            n["primary_cme"]: {"name": f"projects/ernani-demo-argolis/locations/global/certificateMaps/{n['cert_map']}/certificateMapEntries/{n['primary_cme']}"}
        }
        return self

    @staticmethod
    def _presented(inst):
        shown = copy.deepcopy(inst)
        if shown["settings"].get("activationPolicy") == "NEVER":
            shown["state"] = "STOPPED"  # mesmo comportamento do gcloud (DatabaseInstancePresentation)
        return shown

    def _gh(self, argv):
        path = argv[-1]
        if "/actions/workflows/" in path:
            if self.gh_fail:
                return reaper.CmdResult(1, "", "HTTP 503: Service Unavailable")
            status = path.split("status=")[1].split("&")[0]
            runs = self.in_flight.get(status, [])
            return self.ok({"total_count": len(runs), "workflow_runs": runs})
        raise AssertionError(f"chamada gh inesperada: {argv}")

    def _git(self, argv):
        sub = argv[3]
        if sub == "fetch":
            if self.git_fetch_fail:
                return reaper.CmdResult(128, "", "fatal: unable to access 'https://github.com/...': Could not resolve host")
            return reaper.CmdResult(0, "", "")
        if sub == "ls-tree":
            names = ["active-leases/.gitkeep"] + [f"active-leases/{n}" for n in self.remote_leases]
            return reaper.CmdResult(0, "\n".join(names) + "\n", "")
        if sub == "show":
            name = argv[4].split("active-leases/")[1]
            return reaper.CmdResult(0, self.remote_leases[name], "")
        raise AssertionError(f"comando git inesperado: {argv}")

    # ---- inspeção --------------------------------------------------------------------------
    def mutating_calls(self):
        out = []
        for call in self.calls:
            if call[0] != "gcloud":
                continue
            if "patch" in call or "delete" in call or "rm" in call or ("operations" in call and "wait" in call):
                out.append(call)
        return out

    def gclb_deletes(self):
        return [c for c in self.calls if c[0] == "gcloud" and ("delete" in c or c[1:3] == ["storage", "rm"])]

    def count(self, predicate):
        return sum(1 for c in self.calls if predicate(c))


class ReaperTestBase(unittest.TestCase):
    def setUp(self):
        self.world = FakeWorld()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.leases_dir = self.root / "active-leases"
        self.leases_dir.mkdir()
        self.write_local_lease(lease("todo-mobile"))
        self.logs = []

    def tearDown(self):
        self._tmp.cleanup()

    def write_local_lease(self, data, name=None):
        path = self.leases_dir / f"{name or data['workload_slug']}.json"
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def deps(self, grace=10):
        return reaper.ReaperDeps(
            gcp=reaper.Gcp("ernani-demo-argolis", self.world),
            github=reaper.GitHub(REPO, self.world),
            git_leases=reaper.GitLeases(self.root, self.world),
            leases_dir=self.leases_dir,
            region="southamerica-east1",
            run_regions=("southamerica-east1", "us-central1"),
            grace_minutes=grace,
            sleep=lambda seconds: None,
        )

    def run_reaper(self, dry_run=True, grace=10, wait_timeout_s=0):
        return reaper.run_reaper(self.deps(grace), dry_run=dry_run, now_epoch=NOW, log=self.logs.append, wait_timeout_s=wait_timeout_s)

    @staticmethod
    def ev(report, instance=PGSQL):
        return next(e for e in report.evaluations if e.instance == instance)

    @staticmethod
    def failing(ev):
        return {c.check_id for c in ev.checks if not c.ok}


class TestReaperDecisions(ReaperTestBase):
    def test_idle_pgsql_would_stop_in_dry_run(self):
        report = self.run_reaper(dry_run=True)
        ev = self.ev(report)
        self.assertEqual(ev.decision, "would_stop", ev.detail)
        self.assertEqual(ev.action, "stop")
        self.assertEqual(self.failing(ev), set())
        self.assertEqual(
            {c.check_id for c in ev.checks},
            {
                "managed_label",
                "opt_out_label",
                "deletion_protection",
                "running_state",
                "pending_operations",
                "active_leases",
                "in_flight_provisioning",
                "application_databases",
                "cloud_run_references",
                "grace_period",
            },
        )
        self.assertEqual(report.instances_with("would_stop", "would_delete"), [PGSQL])
        self.assertEqual(self.world.mutating_calls(), [])
        self.assertEqual(self.ev(report, "sdlc-shared-mysql").decision, "absent")
        self.assertEqual(self.ev(report, "sdlc-shared-redis").decision, "absent")
        self.assertEqual(report.exit_code, reaper.EXIT_OK)

    def test_idle_pgsql_is_stopped_async_and_never_deleted_in_live_mode(self):
        report = self.run_reaper(dry_run=False)
        ev = self.ev(report)
        self.assertEqual(ev.decision, "stopped", ev.detail)
        self.assertEqual(report.exit_code, reaper.EXIT_OK)
        patches = [c for c in self.world.mutating_calls() if "patch" in c]
        self.assertEqual(len(patches), 1)
        self.assertIn("--activation-policy=NEVER", patches[0])
        self.assertIn("--async", patches[0])
        self.assertIn(PGSQL, patches[0])
        self.assertFalse(any("operations" in c and "wait" in c for c in self.world.calls), "parada assíncrona por padrão")
        self.assertFalse(any("delete" in c and "sql" in c for c in self.world.calls), "Cloud SQL nunca pode ser apagado")
        self.assertEqual(self.world.sql[PGSQL]["settings"]["activationPolicy"], "NEVER")
        self.assertIn("parada solicitada", ev.detail)

    def test_live_stop_can_wait_and_confirm(self):
        ev = self.ev(self.run_reaper(dry_run=False, wait_timeout_s=60))
        self.assertEqual(ev.decision, "stopped")
        self.assertTrue(any("operations" in c and "wait" in c for c in self.world.calls))
        self.assertIn("parada confirmada (activation-policy=NEVER", ev.detail)

    def test_full_reevaluation_happens_right_before_the_mutation(self):
        self.run_reaper(dry_run=False)
        patch_idx = next(i for i, c in enumerate(self.world.calls) if "patch" in c)
        before = self.world.calls[:patch_idx]
        # 2 avaliações completas: 2x git fetch, 2x inventário Cloud Run, 2x databases; + última checagem de runs.
        self.assertEqual(sum(1 for c in before if c[:4] == ["git", "-C", str(self.root), "fetch"]), 2)
        self.assertEqual(sum(1 for c in before if c[1:4] == ["run", "services", "list"]), 4)
        self.assertEqual(sum(1 for c in before if c[1:4] == ["sql", "databases", "list"]), 2)
        last_gh = max(i for i, c in enumerate(before) if c[0] == "gh")
        self.assertGreater(last_gh, max(i for i, c in enumerate(before) if c[1:4] == ["sql", "databases", "list"]))

    def test_application_database_present_skips(self):
        self.world.databases[PGSQL] = ["postgres", "db_cadastro_ativos_ti"]
        report = self.run_reaper(dry_run=False)
        ev = self.ev(report)
        self.assertEqual(ev.decision, "in_use")
        self.assertEqual(self.failing(ev), {"application_databases"})
        self.assertIn("db_cadastro_ativos_ti", ev.detail)
        self.assertEqual(self.world.mutating_calls(), [])
        self.assertEqual(report.exit_code, reaper.EXIT_OK)

    def test_non_pipeline_database_names_are_not_logged(self):
        self.world.databases[PGSQL] = ["postgres", "Folha Pagamento Interna"]
        ev = self.ev(self.run_reaper())
        self.assertEqual(self.failing(ev), {"application_databases"})
        self.assertNotIn("Folha", ev.detail)
        self.assertIn("1 fora do padrão db_<slug>", ev.detail)

    def test_system_databases_are_ignored(self):
        self.world.databases[PGSQL] = ["postgres", "cloudsqladmin", "template0", "template1"]
        self.assertEqual(self.ev(self.run_reaper()).decision, "would_stop")

    def test_active_local_lease_skips(self):
        self.write_local_lease(active_lease("calculadora-simples"))
        ev = self.ev(self.run_reaper(dry_run=False))
        self.assertEqual(ev.decision, "in_use")
        self.assertIn("active_leases", self.failing(ev))
        self.assertIn("calculadora-simples", ev.detail)
        self.assertEqual(self.world.mutating_calls(), [])

    def test_active_remote_lease_skips_even_if_local_checkout_is_stale(self):
        self.world.remote_leases["novo-app.json"] = json.dumps(active_lease("novo-app"))
        ev = self.ev(self.run_reaper())
        self.assertEqual(ev.decision, "in_use")
        self.assertIn("origin/main:novo-app.json", ev.detail)

    def test_expired_but_still_active_lease_is_in_use(self):
        # Lease ACTIVE vencido = teardown ainda não destruiu o workload: o servidor continua em uso.
        self.write_local_lease(lease("vencido", status="ACTIVE", decommissioned=None, expires=NOW - 60))
        self.assertEqual(self.ev(self.run_reaper()).decision, "in_use")

    def test_lease_matched_by_instance_name_variants(self):
        for value in (PGSQL, f"projects/ernani-demo-argolis/instances/{PGSQL}", CONN):
            self.write_local_lease(
                lease("legado", status="PROVISIONING", decommissioned=None, database_type="mysql", expires=0, shared_sql_instance=value)
            )
            self.world.calls.clear()
            self.assertEqual(self.ev(self.run_reaper()).decision, "in_use", value)

    def test_unknown_status_not_expired_is_in_use_and_expired_is_not(self):
        self.write_local_lease(lease("falhou", status="FAILED", decommissioned=None, expires=NOW + 600))
        self.assertEqual(self.ev(self.run_reaper()).decision, "in_use")
        self.write_local_lease(lease("falhou", status="FAILED", decommissioned=None, expires=NOW - 600))
        self.world.calls.clear()
        self.assertEqual(self.ev(self.run_reaper()).decision, "would_stop")

    def test_unreadable_lease_is_fail_safe_and_visible(self):
        self.write_local_lease("{ isto não é json", name="quebrado")
        report = self.run_reaper(dry_run=False)
        self.assertEqual(self.ev(report).decision, "in_use")
        self.assertEqual(self.world.mutating_calls(), [])
        self.assertEqual(report.exit_code, reaper.EXIT_FAIL_SAFE)

    def test_lease_without_recognized_engine_blocks_everything(self):
        self.world.redis = redis_instance()
        self.write_local_lease(active_lease("sem-engine", database_type="", shared_sql_instance=""))
        report = self.run_reaper(dry_run=False)
        self.assertEqual(self.ev(report).decision, "in_use")
        self.assertEqual(self.ev(report, "sdlc-shared-redis").decision, "in_use")
        self.assertEqual(self.world.mutating_calls(), [])

    def test_explicitly_stateless_lease_does_not_hold_sql_but_holds_redis(self):
        self.world.redis = redis_instance()
        self.write_local_lease(active_lease("stateless", database_type="none", shared_sql_instance=""))
        report = self.run_reaper(dry_run=True)
        self.assertEqual(self.ev(report).decision, "would_stop")
        redis_ev = self.ev(report, "sdlc-shared-redis")
        self.assertEqual(redis_ev.decision, "in_use")
        self.assertIn("nenhum lease em uso de qualquer engine", redis_ev.detail)

    def test_in_flight_provisioning_skips(self):
        self.world.in_flight["in_progress"] = [{"id": 4467, "status": "in_progress", "event": "push"}]
        ev = self.ev(self.run_reaper(dry_run=False))
        self.assertEqual(ev.decision, "in_use")
        self.assertEqual(self.failing(ev), {"in_flight_provisioning"})
        self.assertIn("#4467", ev.detail)
        self.assertEqual(self.world.mutating_calls(), [])

    def test_queued_waiting_requested_pending_provisioning_also_skips(self):
        for status in ("queued", "waiting", "requested", "pending"):
            self.world = FakeWorld()
            self.world.in_flight[status] = [{"id": 1, "status": status}]
            self.assertEqual(self.ev(self.run_reaper()).decision, "in_use", status)

    def test_unmanaged_instance_is_never_touched(self):
        self.world.sql[PGSQL]["settings"]["userLabels"] = {"owner": "outra-equipe"}
        ev = self.ev(self.run_reaper(dry_run=False))
        self.assertEqual(ev.decision, "in_use")
        self.assertIn("managed_label", self.failing(ev))
        self.assertEqual(self.world.mutating_calls(), [])

    def test_opt_out_label_keeps_running(self):
        self.world.sql[PGSQL]["settings"]["userLabels"]["finops_idle_reaper"] = "disabled"
        ev = self.ev(self.run_reaper(dry_run=False))
        self.assertEqual(self.failing(ev), {"opt_out_label"})
        self.assertEqual(self.world.mutating_calls(), [])

    def test_deletion_protection_is_respected_with_warning(self):
        self.world.sql[PGSQL]["settings"]["deletionProtectionEnabled"] = True
        ev = self.ev(self.run_reaper(dry_run=False))
        self.assertEqual(ev.decision, "in_use")
        self.assertEqual(self.failing(ev), {"deletion_protection"})
        self.assertTrue(any("deletionProtectionEnabled=true" in w for w in ev.warnings))
        self.assertEqual(self.world.mutating_calls(), [])

    def test_github_api_failure_is_fail_safe(self):
        self.world.gh_fail = True
        report = self.run_reaper(dry_run=False)
        ev = self.ev(report)
        self.assertEqual(ev.decision, "in_use")
        self.assertIn("in_flight_provisioning", self.failing(ev))
        self.assertTrue(report.fail_safe_reasons)
        self.assertEqual(self.world.mutating_calls(), [])
        self.assertTrue(report.to_json()["fail_safe"])
        self.assertEqual(report.exit_code, reaper.EXIT_FAIL_SAFE)

    def test_git_fetch_failure_is_fail_safe(self):
        self.world.git_fetch_fail = True
        report = self.run_reaper(dry_run=False)
        self.assertIn("active_leases", self.failing(self.ev(report)))
        self.assertEqual(self.world.mutating_calls(), [])
        self.assertEqual(report.exit_code, reaper.EXIT_FAIL_SAFE)

    def test_cloud_run_reference_skips_and_names_only_pipeline_services(self):
        self.write_local_lease(lease("app-x"))  # serviço da esteira: prefixo = slug de lease
        self.world.services["southamerica-east1"].append(run_service("app-x-api", annotation=CONN))
        self.world.services["us-central1"].append(run_service("servico-interno-secreto", annotation=CONN))
        ev = self.ev(self.run_reaper(dry_run=False))
        self.assertEqual(self.failing(ev), {"cloud_run_references"})
        self.assertIn("app-x-api", ev.detail)
        self.assertNotIn("servico-interno-secreto", ev.detail)
        self.assertIn("1 serviço(s)/job(s) fora da esteira", ev.detail)
        self.assertEqual(self.world.mutating_calls(), [])

    def test_cloud_run_reference_by_socket_path_or_ip(self):
        self.world.services["us-central1"].append(
            run_service("job-like", env=[{"name": "DATABASE_URL", "value": f"postgresql://u@/db?host=/cloudsql/{CONN}"}])
        )
        self.assertEqual(self.failing(self.ev(self.run_reaper())), {"cloud_run_references"})
        self.world = FakeWorld()
        self.world.jobs["southamerica-east1"].append(run_service("migrador", env=[{"name": "DB_HOST", "value": PGSQL_IP}]))
        self.assertEqual(self.failing(self.ev(self.run_reaper())), {"cloud_run_references"})

    def test_other_instance_reference_does_not_block(self):
        # ponto-api-dev referencia 'ponto-eletronico' (outra instância, fora da esteira).
        self.assertEqual(self.ev(self.run_reaper()).decision, "would_stop")

    def test_cloud_run_inventory_failure_is_fail_safe(self):
        original = self.world._gcloud

        def failing_gcloud(argv):
            if argv[1:4] == ["run", "services", "list"]:
                return reaper.CmdResult(1, "", "PERMISSION_DENIED")
            return original(argv)

        self.world._gcloud = failing_gcloud
        report = self.run_reaper(dry_run=False)
        self.assertIn("cloud_run_references", self.failing(self.ev(report)))
        self.assertEqual(self.world.mutating_calls(), [])
        self.assertEqual(report.exit_code, reaper.EXIT_FAIL_SAFE)

    def test_recent_decommission_within_grace_skips(self):
        self.write_local_lease(lease("recente", decommissioned="2026-11-20T07:30:00Z"))
        ev = self.ev(self.run_reaper(dry_run=False, grace=10))
        self.assertEqual(self.failing(ev), {"grace_period"})
        self.assertEqual(self.world.mutating_calls(), [])
        self.world.calls.clear()
        self.assertEqual(self.ev(self.run_reaper(grace=0)).decision, "would_stop")

    def test_pending_operation_skips(self):
        self.world.operations[PGSQL] = [{"name": "op1", "operationType": "DELETE_DATABASE", "status": "RUNNING"}]
        ev = self.ev(self.run_reaper(dry_run=False))
        self.assertEqual(self.failing(ev), {"pending_operations"})
        self.assertEqual(self.world.mutating_calls(), [])

    def test_already_stopped_instance_is_left_alone(self):
        self.world.sql[PGSQL]["settings"]["activationPolicy"] = "NEVER"
        ev = self.ev(self.run_reaper(dry_run=False))
        self.assertEqual(ev.decision, "already_stopped")
        self.assertEqual(self.world.mutating_calls(), [])
        self.assertFalse(any("databases" in c for c in self.world.calls), "databases list falha com servidor parado")

    def test_credentials_error_is_not_mistaken_for_absent_instance(self):
        self.world.describe_error = "ERROR: (gcloud.auth) The file [/tmp/creds.json] does not exist"
        report = self.run_reaper(dry_run=False)
        ev = self.ev(report)
        self.assertEqual(ev.decision, "error")
        self.assertEqual(self.world.mutating_calls(), [])
        self.assertEqual(report.exit_code, reaper.EXIT_FAIL_SAFE)

    def test_external_strings_are_sanitized_for_public_logs(self):
        self.world.databases[PGSQL] = ["postgres", "db_x\n::add-mask::segredo"]
        report = self.run_reaper()
        text = reaper.render_text(report)
        self.assertNotIn("\n::add-mask::", text)
        for line in text.splitlines():
            self.assertFalse(line.startswith("::"), line)


class TestRevalidationRaces(ReaperTestBase):
    def test_provisioning_starting_mid_run_cancels_stop(self):
        state = {"runs_queries": 0}

        def start_provisioning(world, argv):
            if argv[0] == "gh" and "/actions/workflows/" in argv[-1]:
                state["runs_queries"] += 1
                if state["runs_queries"] > len(reaper.IN_FLIGHT_RUN_STATUSES):
                    world.in_flight["queued"] = [{"id": 99, "status": "queued"}]

        self.world.on_call = start_provisioning
        report = self.run_reaper(dry_run=False)
        ev = self.ev(report)
        self.assertEqual(ev.decision, "skipped_race", ev.detail)
        self.assertEqual(self.world.mutating_calls(), [])
        self.assertEqual(report.exit_code, reaper.EXIT_OK)

    def test_lease_published_after_first_evaluation_cancels_stop(self):
        state = {"fetches": 0}

        def publish_lease(world, argv):
            if argv[0] == "git" and argv[3] == "fetch":
                state["fetches"] += 1
                if state["fetches"] == 2:
                    world.remote_leases["recem-chegado.json"] = json.dumps(active_lease("recem-chegado"))

        self.world.on_call = publish_lease
        ev = self.ev(self.run_reaper(dry_run=False))
        self.assertEqual(ev.decision, "skipped_race")
        self.assertIn("recem-chegado", ev.detail)
        self.assertEqual(self.world.mutating_calls(), [])

    def test_opt_out_label_added_after_first_evaluation_cancels_stop(self):
        state = {"describes": 0}

        def label(world, argv):
            if argv[1:4] == ["sql", "instances", "describe"] and argv[4] == PGSQL:
                state["describes"] += 1
                if state["describes"] == 2:
                    world.sql[PGSQL]["settings"]["userLabels"]["finops_idle_reaper"] = "disabled"

        self.world.on_call = label
        ev = self.ev(self.run_reaper(dry_run=False))
        self.assertEqual(ev.decision, "skipped_race")
        self.assertEqual(self.world.mutating_calls(), [])

    def test_redis_workload_deployed_during_sql_stop_keeps_redis(self):
        # Cenário da revisão adversarial: deploy stateless+Redis termina enquanto o pgsql é parado.
        self.world.redis = redis_instance()

        def deploy_during_stop(world, argv):
            if argv[1:4] == ["sql", "instances", "patch"]:
                world.remote_leases["cache-app.json"] = json.dumps(
                    active_lease("cache-app", database_type="none", shared_sql_instance="", enable_redis=True,
                                 shared_redis_instance="sdlc-shared-redis")
                )

        self.world.on_call = deploy_during_stop
        report = self.run_reaper(dry_run=False)
        self.assertEqual(self.ev(report).decision, "stopped")
        redis_ev = self.ev(report, "sdlc-shared-redis")
        self.assertEqual(redis_ev.decision, "skipped_race", redis_ev.detail)
        self.assertIsNotNone(self.world.redis)
        self.assertFalse(any(c[1:4] == ["redis", "instances", "delete"] for c in self.world.calls))

    def test_failed_stop_returns_action_failed_exit_code(self):
        self.world.fail_patch = True
        report = self.run_reaper(dry_run=False)
        self.assertEqual(self.ev(report).decision, "error")
        self.assertEqual(report.exit_code, reaper.EXIT_ACTION_FAILED)


class TestRedis(ReaperTestBase):
    def test_redis_idle_would_delete_and_is_deleted_live(self):
        self.world.redis = redis_instance()
        ev = self.ev(self.run_reaper(dry_run=True), "sdlc-shared-redis")
        self.assertEqual(ev.decision, "would_delete", ev.detail)
        self.assertEqual(self.world.mutating_calls(), [])

        self.world.calls.clear()
        ev = self.ev(self.run_reaper(dry_run=False), "sdlc-shared-redis")
        self.assertEqual(ev.decision, "deleted", ev.detail)
        deletes = [c for c in self.world.mutating_calls() if c[1:4] == ["redis", "instances", "delete"]]
        self.assertEqual(len(deletes), 1)
        self.assertIn("--region=southamerica-east1", deletes[0])
        self.assertIn("--async", deletes[0])
        self.assertIsNone(self.world.redis)

    def test_redis_kept_while_any_lease_is_in_use(self):
        self.world.redis = redis_instance()
        self.write_local_lease(active_lease("so-postgres"))  # nem usa Redis: mesmo assim segura o Redis
        report = self.run_reaper(dry_run=False)
        self.assertEqual(self.ev(report, "sdlc-shared-redis").decision, "in_use")
        self.assertEqual(self.ev(report).decision, "in_use")
        self.assertEqual(self.world.mutating_calls(), [])

    def test_redis_lease_with_full_resource_path_is_recognized(self):
        view = reaper.evaluate_lease(
            "x", json.dumps(active_lease("r", database_type="none", shared_sql_instance="",
                                         shared_redis_instance=redis_instance()["name"])), NOW
        )
        self.assertEqual(view.engines, frozenset({"redis"}))
        view = reaper.evaluate_lease("y", json.dumps(active_lease("r2", database_type="none", shared_sql_instance="", enable_redis=True)), NOW)
        self.assertEqual(view.engines, frozenset({"redis"}))

    def test_redis_in_use_by_service_reference(self):
        self.world.redis = redis_instance()
        self.world.services["southamerica-east1"].append(run_service("cache-user", env=[{"name": "REDIS_HOST", "value": "10.20.30.40"}]))
        ev = self.ev(self.run_reaper(), "sdlc-shared-redis")
        self.assertEqual(ev.decision, "in_use")
        self.assertEqual(self.failing(ev), {"cloud_run_references"})

    def test_unmanaged_redis_is_never_deleted(self):
        self.world.redis = redis_instance(labels={})
        report = self.run_reaper(dry_run=False)
        self.assertEqual(self.ev(report, "sdlc-shared-redis").decision, "in_use")
        self.assertFalse(any(c[1:4] == ["redis", "instances", "delete"] for c in self.world.calls))


class TestDryRunAndOutputs(ReaperTestBase):
    def test_dry_run_makes_no_mutating_calls_even_with_everything_idle(self):
        self.world.with_gclb()
        self.world.redis = redis_instance()
        self.world.sql["sdlc-shared-mysql"] = pgsql_instance(
            name="sdlc-shared-mysql",
            connectionName="ernani-demo-argolis:southamerica-east1:sdlc-shared-mysql",
            ipAddresses=[],
            settings={"userLabels": {"managed_by": "cloud-provisioning-approvals"}},
        )
        self.world.databases["sdlc-shared-mysql"] = ["mysql", "information_schema", "performance_schema", "sys"]
        report = self.run_reaper(dry_run=True)
        self.assertEqual(
            sorted(report.instances_with("would_stop", "would_delete")),
            ["sdlc-shared-gclb", "sdlc-shared-mysql", "sdlc-shared-pgsql", "sdlc-shared-redis"],
        )
        self.assertEqual(report.instances_with("stopped", "deleted"), [])
        self.assertEqual(self.world.mutating_calls(), [])
        for call in self.world.calls:
            self.assertNotIn("patch", call)
            self.assertNotIn("delete", call)
            self.assertNotIn("rm", call)
        self.assertEqual(len(self.world.gclb), 11)

    def test_outputs_summary_and_json_without_ips(self):
        report = self.run_reaper(dry_run=True)
        out_file = self.root / "gh_output"
        reaper.write_github_output(str(out_file), report)
        outputs = dict(line.split("=", 1) for line in out_file.read_text(encoding="utf-8").splitlines())
        self.assertEqual(outputs["reaper_mode"], "dry-run")
        self.assertEqual(outputs["reaped_count"], "0")
        self.assertEqual(outputs["would_reap_count"], "1")
        self.assertEqual(outputs["would_reap_instances"], PGSQL)
        self.assertEqual(outputs["reaper_fail_safe"], "false")
        text = reaper.render_text(report)
        summary = reaper.render_step_summary(report)
        payload = json.dumps(report.to_json(), ensure_ascii=False)
        self.assertIn("SERIA PARADO", text)
        self.assertIn("sdlc-shared-gclb (gclb, shared-gclb) -> INEXISTENTE", text)
        self.assertIn(f"`{PGSQL}`", summary)
        for blob in (text, summary, payload):
            self.assertNotIn(PGSQL_IP, blob)
            self.assertNotIn("ponto-api-dev", blob)
            self.assertNotIn("google-maps-portal", blob)
        self.assertEqual(report.to_json()["would_reap_instances"], [PGSQL])
        self.assertEqual(report.to_json()["shared_gclb"]["decision"], "absent")


class TestCli(ReaperTestBase):
    def run_main(self, args, env=None):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=False), redirect_stdout(buf):
            rc = reaper.main(args, runner=self.world)
        return rc, buf.getvalue()

    def test_main_rejects_other_projects(self):
        self.assertEqual(reaper.main(["--project", "outro-projeto", "--dry-run", "true"], runner=self.world), reaper.EXIT_USAGE)
        self.assertEqual(reaper.main(["resume", "--project", "outro-projeto", "--engine", "postgresql"], runner=self.world), reaper.EXIT_USAGE)
        self.assertEqual(self.world.calls, [])

    def test_dry_run_defaults_to_true_and_stop_is_async_by_default(self):
        args = reaper._parse_reap_args([])
        self.assertTrue(args.dry_run)
        self.assertEqual(args.grace_minutes, 10)
        self.assertEqual(args.wait_timeout_seconds, 0)
        self.assertFalse(reaper._parse_reap_args(["--dry-run", "false"]).dry_run)

    def test_main_end_to_end_dry_run_writes_outputs_and_summary(self):
        gh_out = self.root / "out.txt"
        summary = self.root / "summary.md"
        rc, stdout = self.run_main(
            ["--dry-run", "true", "--repo-dir", str(self.root), "--leases-dir", str(self.leases_dir), "--github-output", str(gh_out)],
            env={"GITHUB_STEP_SUMMARY": str(summary)},
        )
        self.assertEqual(rc, reaper.EXIT_OK)
        self.assertIn("SERIA PARADO", stdout)
        self.assertIn("--- RELATÓRIO JSON (FINOPS-REAPER) ---", stdout)
        self.assertIn("would_reap_instances=sdlc-shared-pgsql", gh_out.read_text(encoding="utf-8"))
        self.assertIn("FinOps — servidores compartilhados ociosos", summary.read_text(encoding="utf-8"))
        self.assertEqual(self.world.mutating_calls(), [])

    def test_main_returns_fail_safe_exit_code_when_github_is_down(self):
        self.world.gh_fail = True
        rc, stdout = self.run_main(["--dry-run", "false", "--repo-dir", str(self.root), "--leases-dir", str(self.leases_dir)])
        self.assertEqual(rc, reaper.EXIT_FAIL_SAFE)
        self.assertIn("::warning::[FINOPS-REAPER] Fail-safe ativo", stdout)
        self.assertEqual(self.world.mutating_calls(), [])


class TestResume(unittest.TestCase):
    def setUp(self):
        self.world = FakeWorld()
        self.logs = []
        self.clock_value = [0.0]

    def clock(self):
        return self.clock_value[0]

    def sleep(self, seconds):
        self.clock_value[0] += seconds

    def resume(self, phase="full", engine="postgresql", dry_run=False, timeout_s=600, enable_redis=False, repair_gclb=False):
        return reaper.resume_shared_backing(
            reaper.Gcp("ernani-demo-argolis", self.world),
            engine,
            enable_redis=enable_redis,
            phase=phase,
            timeout_s=timeout_s,
            poll_s=15,
            dry_run=dry_run,
            repair_gclb=repair_gclb,
            log=self.logs.append,
            sleep=self.sleep,
            clock=self.clock,
        )

    def patches(self):
        return [c for c in self.world.calls if c[1:4] == ["sql", "instances", "patch"]]

    def stop(self, by=reaper.PIPELINE_SERVICE_ACCOUNT):
        self.world.sql[PGSQL]["settings"]["activationPolicy"] = "NEVER"
        self.world.operations[PGSQL] = [{"name": "op-stop", "operationType": "UPDATE", "status": "DONE", "user": by, "endTime": "2026-11-20T08:00:00Z"}]

    def test_running_instance_needs_nothing(self):
        self.assertEqual(self.resume("full"), 0)
        self.assertEqual(self.patches(), [])

    def test_absent_instance_is_left_to_terraform(self):
        self.world.sql = {}
        self.assertEqual(self.resume("full"), 0)
        self.assertEqual(self.patches(), [])
        self.assertTrue(any("será criada pelo Terraform" in m for m in self.logs))

    def test_stateless_engine_without_redis_is_noop(self):
        self.assertEqual(self.resume("full", engine="none"), 0)
        self.assertEqual(self.world.calls, [])

    def test_stopped_instance_begin_patches_always_without_waiting(self):
        self.stop()
        self.assertEqual(self.resume("begin"), 0)
        self.assertEqual(len(self.patches()), 1)
        self.assertIn("--activation-policy=ALWAYS", self.patches()[0])
        self.assertEqual(self.clock_value[0], 0.0)

    def test_begin_with_pending_operation_does_not_patch(self):
        self.stop()
        self.world.operations[PGSQL].insert(0, {"name": "op-x", "operationType": "UPDATE", "status": "RUNNING"})
        self.assertEqual(self.resume("begin"), 0)
        self.assertEqual(self.patches(), [])

    def test_begin_describe_error_only_warns(self):
        self.world.describe_error = "ERROR: (gcloud.sql.instances.describe) HTTPError 503: backend error"
        self.assertEqual(self.resume("begin"), 0)
        self.assertTrue(any(m.startswith("::warning::") for m in self.logs))

    def test_stopped_instance_wait_patches_and_waits_until_runnable(self):
        self.stop()
        polls = {"n": 0}

        def starting(world, argv):
            # Simula a partida assíncrona: o patch deixa uma operação pendente por 2 consultas.
            if argv[1:4] == ["sql", "instances", "patch"]:
                world.operations[PGSQL] = [{"name": "op", "operationType": "UPDATE", "status": "RUNNING"}]
            if argv[1:4] == ["sql", "operations", "list"] and world.operations[PGSQL][0]["status"] == "RUNNING":
                polls["n"] += 1
                if polls["n"] > 2:
                    world.operations[PGSQL][0]["status"] = "DONE"

        self.world.on_call = starting
        self.assertEqual(self.resume("wait"), 0)
        self.assertEqual(len(self.patches()), 1)
        self.assertGreater(self.clock_value[0], 0)
        self.assertTrue(any("RUNNABLE (activation-policy=ALWAYS) após" in m for m in self.logs))

    def test_patch_failing_then_succeeding_inside_wait(self):
        self.stop()
        attempts = {"n": 0}

        def flaky(world, argv):
            if argv[1:4] == ["sql", "instances", "patch"]:
                attempts["n"] += 1
                world.fail_patch = attempts["n"] == 1

        self.world.on_call = flaky
        self.assertEqual(self.resume("wait"), 0)
        self.assertEqual(len(self.patches()), 2)

    def test_wait_times_out_with_error(self):
        self.stop()
        self.world.fail_patch = True
        self.assertEqual(self.resume("wait", timeout_s=60), 1)
        self.assertTrue(any(m.startswith("::error::") for m in self.logs))

    def test_dry_run_never_patches(self):
        self.stop()
        self.assertEqual(self.resume("full", dry_run=True), 0)
        self.assertEqual(self.patches(), [])
        self.assertTrue(any("executaria" in m for m in self.logs))

    def test_engine_aliases(self):
        self.stop()
        self.assertEqual(self.resume("begin", engine="Postgres"), 0)
        self.assertEqual(len(self.patches()), 1)

    def test_warns_without_exposing_identity_when_stopped_outside_pipeline(self):
        self.stop(by="pessoa@exemplo.gov.br")
        self.assertEqual(self.resume("begin"), 0)
        warning = [m for m in self.logs if "identidade fora da esteira" in m]
        self.assertEqual(len(warning), 1)
        self.assertNotIn("pessoa@exemplo.gov.br", warning[0])

    def test_redis_being_deleted_is_awaited_before_terraform(self):
        self.world.redis = redis_instance(state="DELETING")
        polls = {"n": 0}

        def finishing_delete(world, argv):
            if argv[1:4] == ["redis", "instances", "describe"]:
                polls["n"] += 1
                if polls["n"] > 2:
                    world.redis = None

        self.world.on_call = finishing_delete
        self.assertEqual(self.resume("wait", engine="none", enable_redis=True), 0)
        self.assertGreater(self.clock_value[0], 0)
        self.assertTrue(any("será (re)criado pelo Terraform" in m for m in self.logs))

    def test_redis_ready_is_fine_and_stuck_deleting_times_out(self):
        self.world.redis = redis_instance()
        self.assertEqual(self.resume("wait", engine="postgresql", enable_redis=True), 0)
        self.world.redis = redis_instance(state="DELETING")
        self.assertEqual(self.resume("wait", engine="postgresql", enable_redis=True, timeout_s=30), 1)


EXPECTED_GCLB_DELETE_ORDER = [
    ("compute", "forwarding-rules", "delete", "sdlc-shared-https-fwd"),
    ("compute", "forwarding-rules", "delete", "sdlc-shared-http-fwd"),
    ("compute", "target-https-proxies", "delete", "sdlc-shared-https-proxy"),
    ("compute", "target-http-proxies", "delete", "sdlc-shared-http-proxy"),
    ("compute", "url-maps", "delete", "sdlc-shared-https-urlmap"),
    ("compute", "url-maps", "delete", "sdlc-shared-http-redirect"),
    ("compute", "backend-buckets", "delete", "sdlc-shared-gclb-fallback-be"),
    ("storage", "rm", "--recursive", "gs://sdlc-shared-gclb-fb-270170641495"),
    ("compute", "addresses", "delete", "sdlc-shared-gclb-ip"),
    ("certificate-manager", "maps", "entries", "delete", "sdlc-shared-primary-cme"),
    ("certificate-manager", "maps", "delete", "sdlc-shared-cert-map"),
    ("certificate-manager", "certificates", "delete", "sdlc-shared-bootstrap-cert"),
]


class TestSharedGclb(ReaperTestBase):
    GCLB = "sdlc-shared-gclb"

    def setUp(self):
        super().setUp()
        self.world.with_gclb()

    def delete_sequence(self):
        seq = []
        for call in self.world.gclb_deletes():
            body = [a for a in call[1:] if not a.startswith("--project=") and a != "--format=json" and a not in {"--quiet", "--global", "--location=global"} and not a.startswith("--map=")]
            seq.append(tuple(body))
        return seq

    def test_idle_gclb_would_delete_everything_in_dry_run(self):
        report = self.run_reaper(dry_run=True)
        ev = self.ev(report, self.GCLB)
        self.assertEqual(ev.decision, "would_delete", ev.detail)
        self.assertEqual(ev.action, "teardown_gclb")
        self.assertEqual(ev.state, "12/12 recursos presentes")
        self.assertEqual(reaper.decision_label(ev.kind, ev.decision), "SERIA APAGADO (todos os recursos do GCLB compartilhado, 0 hostRules)")
        self.assertIn("0 hostRules", ev.detail)
        self.assertEqual(self.world.gclb_deletes(), [])

    def test_live_teardown_removes_all_12_in_reverse_dependency_order(self):
        report = self.run_reaper(dry_run=False)
        ev = self.ev(report, self.GCLB)
        self.assertEqual(ev.decision, "deleted", ev.detail)
        self.assertEqual(report.exit_code, reaper.EXIT_OK)
        self.assertEqual(self.delete_sequence(), [tuple(x) for x in EXPECTED_GCLB_DELETE_ORDER])
        self.assertEqual(self.world.gclb, {})
        self.assertEqual(self.world.cert_entries, {})
        self.assertIn("12 recurso(s) removido(s)", ev.detail)
        self.assertIn(self.GCLB, report.instances_with("deleted"))
        # pgsql ocioso também foi parado na mesma execução
        self.assertEqual(self.ev(report).decision, "stopped")

    def test_url_map_with_host_rules_aborts_everything(self):
        self.world = FakeWorld().with_gclb(host_rules=2)
        report = self.run_reaper(dry_run=False)
        ev = self.ev(report, self.GCLB)
        self.assertEqual(ev.decision, "in_use")
        self.assertEqual(self.failing(ev), {"url_map_host_rules"})
        self.assertIn("hostRules=2", ev.detail)
        self.assertEqual(self.world.gclb_deletes(), [])
        self.assertEqual(len(self.world.gclb), 11)

    def test_any_in_use_lease_keeps_gclb(self):
        self.write_local_lease(active_lease("stateless-front", database_type="none", shared_sql_instance=""))
        ev = self.ev(self.run_reaper(dry_run=False), self.GCLB)
        self.assertEqual(ev.decision, "in_use")
        self.assertIn("active_leases", self.failing(ev))
        self.assertEqual(self.world.gclb_deletes(), [])

    def test_in_flight_provisioning_keeps_gclb(self):
        self.world.in_flight["in_progress"] = [{"id": 77, "status": "in_progress"}]
        ev = self.ev(self.run_reaper(dry_run=False), self.GCLB)
        self.assertEqual(self.failing(ev), {"in_flight_provisioning"})
        self.assertEqual(self.world.gclb_deletes(), [])

    def test_ip_used_by_foreign_resource_keeps_gclb(self):
        self.world.gclb["ip"]["users"].append("https://www.googleapis.com/compute/v1/projects/ernani-demo-argolis/global/forwardingRules/outro-lb")
        ev = self.ev(self.run_reaper(dry_run=False), self.GCLB)
        self.assertEqual(self.failing(ev), {"address_users"})
        self.assertEqual(self.world.gclb_deletes(), [])

    def test_opt_out_label_on_ip_keeps_gclb(self):
        self.world.gclb["ip"]["labels"] = {"managed_by": "cloud-provisioning-approvals", "finops_idle_reaper": "disabled"}
        ev = self.ev(self.run_reaper(dry_run=False), self.GCLB)
        self.assertEqual(self.failing(ev), {"opt_out_label"})
        self.assertEqual(self.world.gclb_deletes(), [])

    def test_grace_zero_reaps_right_after_last_teardown(self):
        self.write_local_lease(lease("ultimo", decommissioned="2026-11-20T07:33:00Z"))  # 20 s antes de NOW
        self.assertEqual(self.ev(self.run_reaper(grace=0), self.GCLB).decision, "would_delete")
        self.world.calls.clear()
        ev = self.ev(self.run_reaper(grace=10), self.GCLB)
        self.assertEqual(self.failing(ev), {"grace_period"})

    def test_partial_leftovers_converge_idempotently(self):
        for key in ("https_fwd", "http_fwd", "https_proxy", "http_proxy"):
            del self.world.gclb[key]
        self.world.gclb["ip"]["users"] = []
        report = self.run_reaper(dry_run=False)
        ev = self.ev(report, self.GCLB)
        self.assertEqual(ev.decision, "deleted", ev.detail)
        self.assertEqual(self.world.gclb, {})
        self.assertIn("4 já ausente(s)", ev.detail)
        self.assertEqual(self.delete_sequence(), [tuple(x) for x in EXPECTED_GCLB_DELETE_ORDER[4:]])

    def test_step_failure_stops_reports_error_and_next_run_converges(self):
        self.world.gclb_fail["fallback_backend"] = 3
        report = self.run_reaper(dry_run=False)
        ev = self.ev(report, self.GCLB)
        self.assertEqual(ev.decision, "error")
        self.assertEqual(report.exit_code, reaper.EXIT_ACTION_FAILED)
        self.assertIn("backend bucket sdlc-shared-gclb-fallback-be", ev.detail)
        self.assertIn("fallback_backend", self.world.gclb)
        self.assertNotIn("https_fwd", self.world.gclb)
        self.world.calls.clear()
        report = self.run_reaper(dry_run=False)
        self.assertEqual(self.ev(report, self.GCLB).decision, "deleted")
        self.assertEqual(self.world.gclb, {})

    def test_transient_delete_failure_is_retried(self):
        self.world.gclb_fail["ip"] = 1
        ev = self.ev(self.run_reaper(dry_run=False), self.GCLB)
        self.assertEqual(ev.decision, "deleted", ev.detail)
        self.assertEqual(self.world.gclb, {})

    def test_host_rules_attached_mid_teardown_abort_before_url_map(self):
        def attach_route(world, argv):
            if argv[1:4] == ["compute", "target-http-proxies", "delete"]:
                world.gclb["https_urlmap"]["hostRules"] = [{"hosts": ["novo.nip.io"], "pathMatcher": "pm"}]

        self.world.on_call = attach_route
        report = self.run_reaper(dry_run=False)
        ev = self.ev(report, self.GCLB)
        self.assertEqual(ev.decision, "error")
        self.assertIn("ABORTADO: 1 hostRules", ev.detail)
        self.assertIn("https_urlmap", self.world.gclb)
        self.assertEqual(report.exit_code, reaper.EXIT_ACTION_FAILED)

    def test_orphan_cert_map_entries_of_decommissioned_workloads_are_removed(self):
        self.write_local_lease(lease("app-x"))
        for suffix in ("fe", "bff", "api"):
            name = f"app-x-{suffix}-cme"
            self.world.cert_entries[name] = {"name": f"projects/p/locations/global/certificateMaps/sdlc-shared-cert-map/certificateMapEntries/{name}"}
        ev = self.ev(self.run_reaper(dry_run=True), self.GCLB)
        self.assertEqual(ev.decision, "would_delete")
        entries_check = next(c for c in ev.checks if c.check_id == "cert_map_entries")
        self.assertIn("app-x-api-cme", entries_check.detail)
        self.world.calls.clear()
        self.assertEqual(self.ev(self.run_reaper(dry_run=False), self.GCLB).decision, "deleted")
        self.assertEqual(self.world.cert_entries, {})

    def test_unrelated_argolis_resources_moodle_db_and_other_lbs_are_never_inspected_or_touched(self):
        """Guarantees moodle-db-us-2 and unrelated load balancers in ernani-demo-argolis are NEVER queried or mutated."""
        self.world.sql["moodle-db-us-2"] = pgsql_instance(
            name="moodle-db-us-2",
            connectionName="ernani-demo-argolis:us-central1:moodle-db-us-2",
            settings={"activationPolicy": "ALWAYS", "userLabels": {"env": "demo"}},
        )
        self.world.with_gclb(host_rules=0)
        report = self.run_reaper(dry_run=False)
        self.assertEqual(report.exit_code, reaper.EXIT_OK)
        # moodle-db-us-2 remains untouched in ALWAYS state and is never even passed to gcloud describe/patch/delete
        self.assertEqual(self.world.sql["moodle-db-us-2"]["settings"]["activationPolicy"], "ALWAYS")
        for call in self.world.calls:
            joined = " ".join(call)
            self.assertNotIn("moodle-db-us-2", joined)
        # Unmanaged GCLB (without managed_by=cloud-provisioning-approvals) is also strictly skipped
        self.world = FakeWorld().with_gclb(host_rules=0)
        self.world.gclb["https_fwd"]["labels"] = {}
        self.world.gclb["fallback_bucket"]["labels"] = {}
        self.world.gclb["ip"]["labels"] = {"owner": "other-argolis-demo"}
        report2 = self.run_reaper(dry_run=False)
        gclb_ev = self.ev(report2, self.GCLB)
        self.assertEqual(gclb_ev.decision, "in_use")
        self.assertIn("managed_label", self.failing(gclb_ev))
        self.assertEqual(self.world.gclb_deletes(), [])

    def test_absent_gclb_is_reported_absent(self):
        self.world.gclb, self.world.cert_entries = {}, {}
        ev = self.ev(self.run_reaper(dry_run=False), self.GCLB)
        self.assertEqual(ev.decision, "absent")
        self.assertEqual(self.world.gclb_deletes(), [])

    def test_gclb_inventory_failure_is_fail_safe(self):
        original = self.world._gcloud

        def failing(argv):
            if argv[1:4] == ["compute", "url-maps", "describe"]:
                return reaper.CmdResult(1, "", "ERROR: HTTPError 500: internal")
            return original(argv)

        self.world._gcloud = failing
        report = self.run_reaper(dry_run=False)
        self.assertEqual(self.ev(report, self.GCLB).decision, "error")
        self.assertEqual(self.world.gclb_deletes(), [])
        self.assertEqual(report.exit_code, reaper.EXIT_FAIL_SAFE)


class TestGclbRepairOnDeploy(unittest.TestCase):
    def setUp(self):
        self.world = FakeWorld().with_gclb()
        self.logs = []

    def repair(self, dry_run=False):
        return reaper.repair_partial_gclb(reaper.Gcp("ernani-demo-argolis", self.world), log=self.logs.append, dry_run=dry_run, sleep=lambda s: None)

    def test_functional_gclb_is_reused(self):
        self.assertEqual(self.repair(), 0)
        self.assertEqual(self.world.gclb_deletes(), [])
        self.assertTrue(any("reutilizado" in m for m in self.logs))

    def test_partial_teardown_is_completed_before_terraform(self):
        del self.world.gclb["https_fwd"]
        self.world.gclb["ip"]["users"] = self.world.gclb["ip"]["users"][1:]
        self.assertEqual(self.repair(), 0)
        self.assertEqual(self.world.gclb, {})
        self.assertEqual(self.world.cert_entries, {})
        self.assertTrue(any("concluindo a remoção" in m for m in self.logs))

    def test_partial_with_host_rules_is_left_alone(self):
        del self.world.gclb["https_fwd"]
        self.world.gclb["https_urlmap"]["hostRules"] = [{"hosts": ["a"], "pathMatcher": "b"}]
        self.assertEqual(self.repair(), 0)
        self.assertEqual(self.world.gclb_deletes(), [])
        self.assertTrue(any(m.startswith("::warning::") for m in self.logs))

    def test_dry_run_repair_only_reports(self):
        del self.world.gclb["https_fwd"]
        self.assertEqual(self.repair(dry_run=True), 0)
        self.assertEqual(self.world.gclb_deletes(), [])
        self.assertTrue(any("(dry-run) apagaria" in m for m in self.logs))

    def test_fully_absent_gclb_is_left_to_terraform(self):
        self.world.gclb, self.world.cert_entries = {}, {}
        self.assertEqual(self.repair(), 0)
        self.assertTrue(any("o Terraform recria os 12 recursos" in m for m in self.logs))

    def test_resume_wait_runs_repair_by_default_after_sql_is_ready(self):
        del self.world.gclb["https_fwd"]
        rc = reaper.resume_shared_backing(reaper.Gcp("ernani-demo-argolis", self.world), "postgresql", phase="wait", log=self.logs.append, sleep=lambda s: None)
        self.assertEqual(rc, 0)
        self.assertEqual(self.world.gclb, {})


FAKE_GCLOUD_STUCK = """#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
if args[:3] == ["sql", "instances", "describe"]:
    print(json.dumps({"name": "sdlc-shared-pgsql", "state": "STOPPED", "settings": {"activationPolicy": "NEVER"}}))
elif args[:3] == ["sql", "operations", "list"]:
    print("[]")
elif args[:3] == ["sql", "instances", "patch"]:
    print("ERROR: HTTPError 409: operation in progress", file=sys.stderr)
    sys.exit(1)
else:
    print("comando inesperado: " + " ".join(args), file=sys.stderr)
    sys.exit(3)
"""


class TestPipelineWiring(unittest.TestCase):
    """Guardas do encaixe na esteira: ordem no provision_workload.sh e configuração do workflow."""

    def test_provision_script_resumes_before_build_bundle_upload_and_terraform(self):
        text = (REPO_ROOT / "scripts" / "provision_workload.sh").read_text(encoding="utf-8")
        begin = text.index("--phase=begin")
        wait = text.index("--phase=wait")
        build = text.index("gcloud builds submit")
        upload = text.index('gcloud storage cp "${TF_BUNDLE}"')
        reconcile = text.index('"${SRC_DIR}/infra/terraform/reconcile_shared_state.sh" \\')
        apply_ = text.index('terraform -chdir="${SRC_DIR}/infra/terraform" apply')
        self.assertLess(begin, build)
        self.assertLess(wait, upload)
        self.assertLess(wait, reconcile)
        self.assertLess(wait, apply_)
        self.assertEqual(text.count('--enable-redis="${ENABLE_REDIS,,}"'), 2)
        self.assertIn('"enable_redis": $([[ "${ENABLE_REDIS,,}" == "true" ]] && echo true || echo false),', text)

    def test_wait_failure_aborts_provisioning_shell_before_terraform(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = pathlib.Path(tmp) / "gcloud"
            fake.write_text(FAKE_GCLOUD_STUCK, encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                python3 "{SCRIPT}" resume --project=ernani-demo-argolis --engine=postgresql --enable-redis=false \\
                  --phase=wait --timeout-seconds=1 --poll-seconds=1 --dry-run=false
                echo TERRAFORM_WOULD_RUN
                """
            )
            env = dict(os.environ, PATH=f"{tmp}{os.pathsep}{os.environ.get('PATH', '')}", PYTHONDONTWRITEBYTECODE="1")
            proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60, check=False)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("TERRAFORM_WOULD_RUN", proc.stdout)
        self.assertIn("::error::", proc.stdout)

    def test_teardown_workflow_wiring(self):
        text = (REPO_ROOT / ".github" / "workflows" / "ttl-and-budget-teardown.yml").read_text(encoding="utf-8")
        self.assertLess(text.index("Commit Decommission Audit Trail to main"), text.index("python3 scripts/reap_idle_shared_backing.py"))
        self.assertIn("github-oidc-approvals", text)
        try:
            import yaml  # noqa: PLC0415
        except ImportError:  # PyYAML ausente: validações estruturais abaixo dependem dele
            self.skipTest("PyYAML indisponível")
        wf = yaml.safe_load(text)
        on = wf.get("on", wf.get(True))
        self.assertEqual(on["schedule"], [{"cron": "41 * * * *"}])
        self.assertEqual(wf["permissions"], {"contents": "write", "id-token": "write", "actions": "read"})
        self.assertEqual(wf["concurrency"]["group"], "ttl-teardown-ernani-demo-argolis")
        steps = {s["name"]: s for s in wf["jobs"]["teardown-expired-workloads"]["steps"]}
        auth = steps["Authenticate to Google Cloud (ernani-demo-argolis) via Keyless OIDC WIF"]
        self.assertNotIn("if", auth)
        self.assertEqual(auth["id"], "auth")
        self.assertNotIn("if", steps["Set up Google Cloud SDK"])
        check = steps["Check if any active lease requires teardown"]
        self.assertIn("REPORT_ONLY", check["env"])
        self.assertIn('print("needs_teardown=false")', check["run"])
        reap = steps["FinOps — Stop/Remove Idle Shared Infra (DB servers, Redis, GCLB) when no workload uses them"]
        self.assertIn("!cancelled()", reap["if"])
        self.assertIn("steps.auth.outcome == 'success'", reap["if"])
        self.assertIn("vars.FINOPS_REAPER_ENABLED != 'false'", reap["if"])
        self.assertIn("vars.FINOPS_REAPER_DRY_RUN == 'true'", reap["env"]["REAP_DRY_RUN"])
        self.assertEqual(reap["env"]["GH_TOKEN"], "${{ github.token }}")
        self.assertIn('--grace-minutes "0"', reap["run"])


if __name__ == "__main__":
    unittest.main()
