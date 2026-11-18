"""Celery entry point: reads the environment once and builds the app.

celery -A app.celery worker --concurrency=1
celery -A app.celery beat
"""

from app.config import load_worker_settings
from app.worker import create_app

app = create_app(load_worker_settings())
