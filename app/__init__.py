from pathlib import Path

from flask import Flask


def create_app():
    app = Flask(__name__)
    root = Path(__file__).resolve().parent.parent
    app.config["OUTPUT_DIR"] = root / "output"
    app.config["OUTPUT_DIR"].mkdir(exist_ok=True)
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

    from .routes import bp
    app.register_blueprint(bp)
    return app
