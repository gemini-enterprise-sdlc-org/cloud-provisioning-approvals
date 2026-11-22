#!/usr/bin/env python3
"""Idempotent Atomic URL Map Reconciler for Shared Singleton GCLB (`sdlc-shared-https-urlmap`).

Registers (`upsert`) or detaches (`remove`) a single workload's `.nip.io` hostRules & pathMatchers
(`<prefix>-frontend.<ip>.nip.io`, `<prefix>-backend.<ip>.nip.io`, `<prefix>-api.<ip>.nip.io`)
on `sdlc-shared-https-urlmap` via Google Compute Engine REST API optimistic locking (`fingerprint`),
preserving 100% of sibling workloads' routes.

Concurrency hardening: provisioning and TTL teardown workflows run in different concurrency groups,
so two workloads may patch the shared URL Map at the same time. Every attempt re-reads the URL Map
(fresh `fingerprint`), recomputes the desired routes and retries with backoff on optimistic-lock
conflicts (HTTP 412/409), `resourceNotReady`, throttling (429/503) or slow global operations.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

URL_MAP_NAME = "sdlc-shared-https-urlmap"
MAX_ATTEMPTS = 6
RETRYABLE_MARKERS = (
    "(412)",
    "(409)",
    "(429)",
    "(503)",
    "resourceNotReady",
    "RESOURCE_NOT_READY",
    "conditionNotMet",
    "CONDITION_NOT_MET",
    "fingerprint",
)


def _access_token() -> str:
    proc = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _api_request(
    method: str, url: str, token: str, payload: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        if err.code == 404 and method == "GET":
            return None
        detail = err.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Compute API {method} {url} failed ({err.code}): {detail}") from err


def _wait_global_operation(project: str, op_name: str, token: str) -> None:
    op_url = f"https://compute.googleapis.com/compute/v1/projects/{project}/global/operations/{op_name}"
    for _ in range(90):
        op = _api_request("GET", op_url, token) or {}
        if op.get("status") == "DONE":
            if "error" in op:
                raise RuntimeError(f"Global operation {op_name} failed: {op['error']}")
            return
        time.sleep(2)
    raise TimeoutError(f"Timed out waiting for global operation {op_name}")


def _belongs_to_slug(short_slug: str, pm_name: str, hosts: list[str]) -> bool:
    if pm_name.startswith(f"pm-{short_slug}-"):
        return True
    prefixes = (
        f"{short_slug}-frontend.",
        f"{short_slug}-backend.",
        f"{short_slug}-api.",
    )
    return any(any(h.startswith(pfx) for pfx in prefixes) for h in hosts)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    msg = str(exc)
    return any(marker in msg for marker in RETRYABLE_MARKERS)


def _build_patch_body(
    current: dict[str, Any],
    args: argparse.Namespace,
    base_compute: str,
    short: str,
) -> tuple[dict[str, Any], int]:
    kept_host_rules = [
        hr
        for hr in current.get("hostRules", [])
        if not _belongs_to_slug(short, str(hr.get("pathMatcher", "")), list(hr.get("hosts", [])))
    ]
    kept_matchers = [
        pm
        for pm in current.get("pathMatchers", [])
        if not str(pm.get("name", "")).startswith(f"pm-{short}-")
    ]

    if args.action == "upsert":
        if not args.gclb_ip:
            raise ValueError("--gclb-ip obrigatorio em upsert")
        uses_prefix = args.api_uses_global_prefix.strip().lower() == "true"
        sep_bff = args.has_separate_bff.strip().lower() == "true"

        spa_be = f"{base_compute}/backendBuckets/{short}-spa-be"
        api_be = f"{base_compute}/backendServices/{short}-api-be"
        bff_be = f"{base_compute}/backendServices/{short}-bff-be" if sep_bff else api_be

        fe_host = f"{short}-frontend.{args.gclb_ip}.nip.io"
        bff_host = f"{short}-backend.{args.gclb_ip}.nip.io"
        api_host = f"{short}-api.{args.gclb_ip}.nip.io"

        fe_api_rule: dict[str, Any] = {
            "paths": ["/api", "/api/*"],
            "service": bff_be,
        }
        if not uses_prefix:
            fe_api_rule["routeAction"] = {"urlRewrite": {"pathPrefixRewrite": "/"}}

        api_matcher: dict[str, Any] = {
            "name": f"pm-{short}-api",
            "defaultService": api_be,
        }
        if not uses_prefix:
            api_matcher["pathRules"] = [
                {
                    "paths": ["/api", "/api/*"],
                    "service": api_be,
                    "routeAction": {"urlRewrite": {"pathPrefixRewrite": "/"}},
                }
            ]

        kept_host_rules.extend(
            [
                {"hosts": [fe_host], "pathMatcher": f"pm-{short}-fe"},
                {"hosts": [bff_host], "pathMatcher": f"pm-{short}-bff"},
                {"hosts": [api_host], "pathMatcher": f"pm-{short}-api"},
            ]
        )
        kept_matchers.extend(
            [
                {
                    "name": f"pm-{short}-fe",
                    "defaultService": spa_be,
                    "pathRules": [fe_api_rule],
                },
                {
                    "name": f"pm-{short}-bff",
                    "defaultService": bff_be,
                },
                api_matcher,
            ]
        )

    patch_body = {
        "fingerprint": current["fingerprint"],
        "defaultService": current["defaultService"],
        "hostRules": kept_host_rules,
        "pathMatchers": kept_matchers,
    }
    return patch_body, len(kept_host_rules)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile per-workload routes on shared GCLB URL Map.")
    parser.add_argument("action", choices=("upsert", "remove"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--short-slug", required=True)
    parser.add_argument("--gclb-ip", default="")
    parser.add_argument("--api-uses-global-prefix", default="true")
    parser.add_argument("--has-separate-bff", default="false")
    args = parser.parse_args(argv)

    token = _access_token()
    base_compute = f"https://compute.googleapis.com/compute/v1/projects/{args.project}/global"
    map_url = f"{base_compute}/urlMaps/{URL_MAP_NAME}"
    short = args.short_slug.strip().lower()[:24].rstrip("-")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        current = _api_request("GET", map_url, token)
        if current is None:
            if args.action == "remove":
                print(f"[GCLB-ROUTES] URL Map '{URL_MAP_NAME}' inexistente; nada a remover.")
                return 0
            raise RuntimeError(f"URL Map '{URL_MAP_NAME}' não encontrado em {args.project}.")

        patch_body, active_rules = _build_patch_body(current, args, base_compute, short)
        try:
            op = _api_request("PATCH", map_url, token, patch_body) or {}
            if "name" in op:
                _wait_global_operation(args.project, str(op["name"]), token)
        except (RuntimeError, TimeoutError) as exc:
            if attempt >= MAX_ATTEMPTS or not _is_retryable(exc):
                raise
            delay = 5 * attempt
            print(
                f"[GCLB-ROUTES] Alteração concorrente no URL Map '{URL_MAP_NAME}' "
                f"(tentativa {attempt}/{MAX_ATTEMPTS}): {str(exc)[:180]} — relendo fingerprint em {delay}s...",
                file=sys.stderr,
            )
            time.sleep(delay)
            continue

        print(
            f"[GCLB-ROUTES] {args.action.upper()} concluido para '{short}' em '{URL_MAP_NAME}' "
            f"(total hostRules ativas={active_rules}, tentativa {attempt}/{MAX_ATTEMPTS})."
        )
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
