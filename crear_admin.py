#!/usr/bin/env python3
"""Crea la cuenta de administrador (la primera, o una más).

La contraseña se pide por terminal SIN mostrarla y se guarda únicamente su
hash scrypt: no se escribe en disco, ni en el historial del shell, ni en
ningún log. Por eso no se acepta como argumento.

    python crear_admin.py                         # pregunta usuario, correo y contraseña
    python crear_admin.py marco --email m@x.com   # solo pregunta la contraseña

Sin terminal (por ejemplo, al desplegar), se leen las variables de entorno
ADMIN_USERNAME, ADMIN_EMAIL (opcional) y ADMIN_PASSWORD. Nunca escribas la
contraseña en un archivo del repositorio.

Si el usuario ya existe, no se toca: para cambiarle la contraseña usa el
panel /admin o el enlace de «¿Olvidaste tu contraseña?».
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

import core


def _preguntar(texto: str, defecto: str = "") -> str:
    if defecto:
        return defecto
    if not sys.stdin.isatty():
        return ""
    return input(texto).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("username", nargs="?", default="", help="usuario para entrar")
    ap.add_argument("--email", default="", help="correo (opcional; sirve para restablecer)")
    ap.add_argument("--nombre", default="", help="nombre visible (opcional)")
    args = ap.parse_args()

    username = _preguntar("Usuario: ", args.username or os.environ.get("ADMIN_USERNAME", ""))
    email = _preguntar("Correo (opcional): ", args.email or os.environ.get("ADMIN_EMAIL", ""))
    if not username:
        print("Falta el usuario (argumento o ADMIN_USERNAME).", file=sys.stderr)
        return 1

    db = core.DatabaseManager()
    if db.find_user(username) or (email and db.find_user_by_email(email)):
        print("Ya existe una cuenta con ese usuario o ese correo. No se cambió nada.",
              file=sys.stderr)
        return 1

    clave = os.environ.get("ADMIN_PASSWORD", "")
    if not clave:
        try:
            clave = getpass.getpass("Contraseña: ")
            otra = getpass.getpass("Repítela  : ")
        except (KeyboardInterrupt, EOFError):
            print("\nCancelado.", file=sys.stderr)
            return 1
        if clave != otra:
            print("No coinciden.", file=sys.stderr)
            return 1

    try:
        uid = db.create_user(username=username, password=clave, email=email,
                             role="admin", status="active", nombre=args.nombre)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"Listo: administrador «{username}» creado (id {uid}). Entra en /entrar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
