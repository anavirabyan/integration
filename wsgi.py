"""gunicorn entry point:  gunicorn wsgi:app"""
from integration.app import create_app, setup_logging
from integration.config import settings

setup_logging(settings.log_level)
app = create_app()
