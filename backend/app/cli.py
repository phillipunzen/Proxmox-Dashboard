"""Small admin CLI for emergencies (run inside the container).

    docker compose exec dashboard python -m app.cli reset-user admin
    docker compose exec dashboard python -m app.cli list-users
"""

from __future__ import annotations

import argparse
import asyncio
import secrets

from sqlalchemy import select

from app.auth.service import AuthService
from app.config import get_settings
from app.core.crypto import Encryptor, load_or_create_app_key
from app.db.engine import Database
from app.db.models import User


async def _service() -> tuple[Database, AuthService]:
    s = get_settings()
    db = Database.for_data_dir(s.data_dir)
    await db.create_all()
    key = load_or_create_app_key(s.data_dir, s.app_secret_key.get_secret_value())
    return db, AuthService(db, s, Encryptor(key))


async def reset_user(username: str) -> None:
    """New temporary password, TOTP off, lockout cleared, all sessions ended."""
    db, svc = await _service()
    async with db.session() as s:
        user = await s.scalar(select(User).where(User.username == username))
        if user is None:
            raise SystemExit(f"Benutzer '{username}' nicht gefunden")
        user.disabled = False
        user.locked_until = None
        user.failed_logins = 0
        await s.commit()
    temp = secrets.token_urlsafe(15)
    await svc.set_password(user.id, temp, temporary=True)
    await svc.disable_totp(user.id)
    await svc.revoke_sessions(user.id)
    print(f"Vorläufiges Passwort für {username}: {temp}")
    print("TOTP wurde zurückgesetzt; beim nächsten Login neu einrichten.")
    await db.dispose()


async def list_users() -> None:
    db, svc = await _service()
    for u in await svc.list_users():
        flags = [u.role, u.auth_source, "TOTP" if u.totp_enabled else "",
                 "deaktiviert" if u.disabled else ""]
        print(f"{u.username:24} {' '.join(f for f in flags if f)}")
    await db.dispose()


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reset-user", help="Passwort + TOTP eines Benutzers zurücksetzen")
    r.add_argument("username")
    sub.add_parser("list-users", help="Benutzer auflisten")
    args = p.parse_args()
    if args.cmd == "reset-user":
        asyncio.run(reset_user(args.username))
    else:
        asyncio.run(list_users())


if __name__ == "__main__":
    main()
