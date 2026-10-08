import os
import sys
import tempfile
from pathlib import Path

# Point the app at a throwaway database before it is imported.
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture(scope="session")
def client():
    return TestClient(main.app)


def login(client, email, password):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture()
def customer(client):
    return login(client, "prashast@odts.com", "pass123")


@pytest.fixture()
def admin(client):
    return login(client, "admin@odts.com", "admin123")


@pytest.fixture()
def agent(client):
    return login(client, "ravi@odts.com", "agent123")
