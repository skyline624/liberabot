#!/usr/bin/env python3
"""Verifie qu'un basculement en mode natif fonctionnerait, sans rien poster.

Le mode natif poste avec le token du compte, pas avec un webhook : le compte doit
donc etre membre du serveur de destination et pouvoir y ecrire. Ce script controle
cet acces pour chaque regle de config.json (lecture seule, aucun message envoye).

Usage:
    PYTHONPATH=. venv/bin/python scripts/check_native_forward.py
    PYTHONPATH=. venv/bin/python scripts/check_native_forward.py --all

Sans --all, seules les regles deja en mode natif sont verifiees. Avec --all,
toutes les regles sont testees comme si le mode natif etait active partout, ce
qui permet de valider avant de basculer.

Le token n'est jamais affiche.
"""
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from discordless.config import Config
from discordless.native_forward import API_BASE, client_headers, resolve_token

_CHANNEL_KINDS = {
    0: "salon texte",
    5: "salon d'annonces",
    10: "thread d'annonce",
    11: "thread public",
    12: "thread prive",
    15: "forum",
}


def _get(url, token):
    """GET une ressource API. Retourne (status, payload|texte)."""
    try:
        resp = requests.get(url, headers=client_headers(token), timeout=15)
    except requests.RequestException as exc:
        return -1, str(exc)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, resp.text[:200]


def check_destination(dest, token):
    """Retourne (ok, message) pour un salon de destination."""
    status, data = _get(f"{API_BASE}/channels/{dest}", token)
    if status == 200:
        kind = _CHANNEL_KINDS.get(data.get("type"), f"type {data.get('type')}")
        name = data.get("name") or "(sans nom)"
        if data.get("type") in (10, 11, 12) and data.get("thread_metadata", {}).get("archived"):
            return False, f"OK mais thread ARCHIVE — #{name} ({kind}), a rouvrir avant bascule"
        return True, f"#{name} ({kind})"
    if status == 401:
        return False, "token invalide ou expire (401)"
    if status == 403:
        return False, "acces refuse (403) — le compte n'est pas membre ou n'a pas la permission"
    if status == 404:
        return False, "salon introuvable (404) — mauvais id, ou compte hors du serveur"
    return False, f"HTTP {status}: {str(data)[:120]}"


def main():
    check_all = "--all" in sys.argv
    cfg = Config.load()

    token = resolve_token(cfg.user_token)
    if not token:
        sys.exit(
            "Aucun token trouve. Renseignez 'user_token' dans config.json, ou lancez "
            "ce script sur la machine ou tourne le client Discord."
        )
    source = "config.json" if cfg.user_token else "client Discord local"

    status, me = _get(f"{API_BASE}/users/@me", token)
    if status != 200:
        sys.exit(f"Token invalide (HTTP {status}) — source: {source}")
    print(f"Token OK (source: {source}) — compte @{me.get('username')} ({me.get('id')})")
    print(f"Mode global: {cfg.forward_mode}")
    print()

    rules = [r for r in cfg.forwards if check_all or r.native]
    if not rules:
        print("Aucune regle en mode natif. Relancez avec --all pour tester la bascule.")
        return 0

    failures = 0
    for rule in rules:
        label = rule.webhook_username or ",".join(rule.channels)
        dest = rule.destination
        if not dest:
            print(f"[KO] {label}: aucune destination — renseignez 'dest_channel_id'")
            failures += 1
            continue
        ok, detail = check_destination(dest, token)
        print(f"[{'OK' if ok else 'KO'}] {label} -> {dest} : {detail}")
        if not ok:
            failures += 1

    print()
    if failures:
        print(f"{failures} regle(s) bloqueraient en mode natif — ne pas basculer en l'etat.")
        return 1
    print(f"{len(rules)} regle(s) pretes pour le mode natif.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
