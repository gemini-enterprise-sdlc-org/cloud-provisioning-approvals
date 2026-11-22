#!/usr/bin/env python3
"""Verifies .github/CODEOWNERS syntax and enforces that the merging actor is a registered Code Owner."""
import os
import pathlib
import re
import sys


def parse_codeowners(path: pathlib.Path) -> set[str]:
    if not path.is_file():
        print("ERRO FATAL: Arquivo .github/CODEOWNERS ausente!", file=sys.stderr)
        sys.exit(1)
    owners: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        for token in line.split()[1:]:
            if token.startswith("@"):
                owners.add(token.lstrip("@").lower())
    if not owners:
        print("ERRO FATAL: Nenhum aprovador (@handle) configurado em .github/CODEOWNERS!", file=sys.stderr)
        sys.exit(1)
    return owners


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "validate"
    owners = parse_codeowners(pathlib.Path(".github/CODEOWNERS"))
    print(f"CODEOWNERS registrados: {', '.join('@' + o for o in sorted(owners))}")
    if mode == "enforce-actor":
        actor = os.environ.get("GITHUB_ACTOR", "").strip().lower()
        if not actor:
            print("ERRO FATAL: GITHUB_ACTOR vazio!", file=sys.stderr)
            sys.exit(1)
        if actor not in owners:
            print(
                f"BLOQUEIO CODEOWNERS: O usuário '@{actor}' que realizou o merge/disparo NÃO está listado em .github/CODEOWNERS ({', '.join('@' + o for o in sorted(owners))})!",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"CODEOWNERS PASS: Aprovador '@{actor}' autorizado por .github/CODEOWNERS!")


if __name__ == "__main__":
    main()
