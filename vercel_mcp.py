from __future__ import annotations

import os

from app.mcp_server.vercel_app import create_vercel_mcp_app


app = create_vercel_mcp_app(os.environ)
