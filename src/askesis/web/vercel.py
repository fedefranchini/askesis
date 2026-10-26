"""Entry point of the hosted dashboard (Vercel). Configuration only from environment variables:
ASKESIS_DB_URL and ASKESIS_DB_TOKEN (Turso), ASKESIS_WEB_ORIGIN (https://…), ASKESIS_CONFIG_TOML (private settings).
"""

from askesis.web.app import create_app

app = create_app()
