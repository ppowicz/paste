#!/usr/bin/env python3
"""Minimal Pastebin-like HTTP service backed by SQLite."""

from __future__ import annotations

import argparse
import html
import os
import secrets
import sqlite3
import string
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List, Optional
from urllib.parse import parse_qs


DB_PATH = Path(__file__).with_name("pastes.sqlite3")
TEMPLATES_DIR = Path(__file__).with_name("templates")
SLUG_ALPHABET = string.ascii_letters + string.digits
SLUG_LENGTH = 6


def _load_template(name: str) -> str:
    path = TEMPLATES_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"Missing template: {path}")
    return path.read_text(encoding="utf-8")


HOME_TEMPLATE = _load_template("home.html")
PASTE_TEMPLATE = _load_template("paste.html")


def init_db() -> None:
    """Ensure the SQLite database exists with the expected schema."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pastes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                title TEXT,
                content TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()


def _generate_slug() -> str:
    return "".join(secrets.choice(SLUG_ALPHABET) for _ in range(SLUG_LENGTH))


def create_paste(title: str, content: str) -> Optional[str]:
    content = (content or "").strip()
    if not content:
        return None

    title = (title or "").strip() or None
    with sqlite3.connect(DB_PATH) as conn:
        for _ in range(10):  # make a few attempts to avoid slug collisions
            slug = _generate_slug()
            try:
                conn.execute(
                    "INSERT INTO pastes (slug, title, content) VALUES (?, ?, ?)",
                    (slug, title, content),
                )
                conn.commit()
                return slug
            except sqlite3.IntegrityError:
                continue
    return None


@dataclass
class Paste:
    slug: str
    title: Optional[str]
    content: str
    created_at: datetime


def get_paste(slug: str) -> Optional[Paste]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT slug, title, content, created_at FROM pastes WHERE slug = ?",
            (slug,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return Paste(
            slug=row["slug"],
            title=row["title"],
            content=row["content"],
            created_at=datetime.fromisoformat(row["created_at"])
            if isinstance(row["created_at"], str)
            else row["created_at"],
        )


def list_recent(limit: int = 10) -> List[Paste]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT slug, title, content, created_at
            FROM pastes
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cur.fetchall()
    pastes: List[Paste] = []
    for row in rows:
        pastes.append(
            Paste(
                slug=row["slug"],
                title=row["title"],
                content=row["content"],
                created_at=datetime.fromisoformat(row["created_at"])
                if isinstance(row["created_at"], str)
                else row["created_at"],
            )
        )
    return pastes


def render_homepage() -> str:
    return HOME_TEMPLATE


def render_paste_page(paste: Paste) -> str:
    title = html.escape(paste.title or paste.slug)
    content = html.escape(paste.content)
    created = paste.created_at.strftime("%Y-%m-%d %H:%M:%S")
    return (
        PASTE_TEMPLATE.replace("{{TITLE}}", title)
        .replace("{{CREATED_AT}}", created)
        .replace("{{SLUG}}", paste.slug)
        .replace("{{CONTENT}}", content)
    )


class PasteRequestHandler(BaseHTTPRequestHandler):
    server_version = "PasteServer/1.0"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/" or self.path == "/index.html":
            body = render_homepage()
            self._send_html(body)
            return

        if self.path.startswith("/raw/"):
            slug = self.path[len("/raw/") :]
            self._serve_raw(slug)
            return

        slug = self.path.lstrip("/")
        if slug:
            paste = get_paste(slug)
            if paste:
                self._send_html(render_paste_page(paste))
                return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/create":
            form = self._parse_form()
            slug = create_paste(
                form.get("title", ""),
                form.get("content", ""),
            )
            if slug:
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", f"/{slug}")
                self.end_headers()
                return
            self.send_error(HTTPStatus.BAD_REQUEST, "Content required")
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, *args, **kwargs):  # noqa: D401
        return

    def _parse_form(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        data = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else ""
        return {k: v[0] for k, v in parse_qs(data).items() if v}

    def _send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _serve_raw(self, slug: str) -> None:
        paste = get_paste(slug)
        if not paste:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        encoded = paste.content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def run_server(host: str, port: int) -> None:
    init_db()
    with ThreadingHTTPServer((host, port), PasteRequestHandler) as httpd:
        print(f"[paste] Listening on http://{host}:{port}")
        httpd.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="SQLite-backed paste server")
    parser.add_argument("--host", default=os.getenv("PASTE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PASTE_PORT", "6002")))
    args = parser.parse_args()

    run_server(args.host, args.port)


if __name__ == "__main__":
    main()