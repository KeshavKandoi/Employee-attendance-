import shutil
import socket
import subprocess
import tempfile
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.main import app


@pytest.fixture(scope="session")
def mongo_server():
    if not shutil.which("mongod"):
        pytest.skip("mongod is unavailable")
    with tempfile.TemporaryDirectory(prefix="hrone-test-") as directory:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        uri = f"mongodb://127.0.0.1:{port}"
        process = subprocess.Popen(
            ["mongod", "--dbpath", directory, "--port", str(port), "--bind_ip", "127.0.0.1", "--quiet"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        client = MongoClient(uri, serverSelectionTimeoutMS=500)
        try:
            for _ in range(50):
                try:
                    client.admin.command("ping")
                    break
                except Exception:
                    if process.poll() is not None:
                        pytest.skip("isolated mongod could not start")
                    time.sleep(0.1)
            else:
                pytest.skip("isolated mongod did not become ready")
            yield uri
        finally:
            client.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


@pytest.fixture
def api(mongo_server, monkeypatch):
    db_name = "hrone_test_" + uuid.uuid4().hex
    monkeypatch.setenv("MONGO_URI", mongo_server)
    monkeypatch.setenv("MONGO_DB", db_name)
    try:
        with TestClient(app) as client:
            yield client, app.state.db
    finally:
        MongoClient(mongo_server).drop_database(db_name)
