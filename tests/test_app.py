"""Pruebas automatizadas para la aplicación de administración de usuarios."""

import io
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app import database, main, s3_service
from app.database import Base, get_db
from app.models import User

# Motor SQLite en memoria compartido entre hilos/sesiones para pruebas rápidas y aisladas
TEST_ENGINE = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=TEST_ENGINE)

database.engine = TEST_ENGINE
main.engine = TEST_ENGINE


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


main.app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(autouse=True)
def reset_environment_and_db(monkeypatch):
    """Reinicia la base de datos en memoria y configura variables de entorno por prueba."""
    Base.metadata.drop_all(bind=TEST_ENGINE)
    Base.metadata.create_all(bind=TEST_ENGINE)

    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "supersecret123")
    monkeypatch.setenv("S3_BUCKET_NAME", "test-private-avatars-bucket")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "")
    yield
    Base.metadata.drop_all(bind=TEST_ENGINE)


@pytest.fixture
def mock_s3(monkeypatch):
    """Mockea boto3.client para interceptar llamadas a S3 sin requerir red ni credenciales AWS."""
    s3_mock = MagicMock()
    s3_mock.generate_presigned_url.side_effect = (
        lambda operation, Params, ExpiresIn: f"https://s3.mock.local/{Params['Bucket']}/{Params['Key']}?exp={ExpiresIn}"
    )
    boto_client_mock = MagicMock(return_value=s3_mock)
    monkeypatch.setattr(s3_service.boto3, "client", boto_client_mock)
    return s3_mock, boto_client_mock


@pytest.fixture
def client(mock_s3):
    """Cliente de pruebas FastAPI."""
    with TestClient(main.app) as test_client:
        yield test_client


def _login(client: TestClient, username: str = "admin", password: str = "supersecret123"):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


def test_healthcheck(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "admin-users-app"}


def test_protected_routes_require_session(client: TestClient):
    routes = [
        ("GET", "/"),
        ("POST", "/users/create"),
        ("GET", "/users/1/edit"),
        ("POST", "/users/1/edit"),
        ("POST", "/users/1/delete"),
    ]
    for method, path in routes:
        response = client.request(method, path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_login_logout_flow(client: TestClient):
    login_page = client.get("/login")
    assert login_page.status_code == 200
    assert "Panel de Administrador" in login_page.text

    bad_login = _login(client, username="admin", password="wrong-password")
    assert bad_login.status_code == 401
    assert "Credenciales inválidas" in bad_login.text

    good_login = _login(client)
    assert good_login.status_code == 303
    assert good_login.headers["location"] == "/"

    dashboard = client.get("/", follow_redirects=False)
    assert dashboard.status_code == 200
    assert "Directorio de usuarios" in dashboard.text

    logout_resp = client.post("/logout", follow_redirects=False)
    assert logout_resp.status_code == 303
    assert logout_resp.headers["location"] == "/login"

    after_logout = client.get("/", follow_redirects=False)
    assert after_logout.status_code == 303
    assert after_logout.headers["location"] == "/login"


def test_crud_users_and_s3_lifecycle(client: TestClient, mock_s3):
    s3_mock, _ = mock_s3
    _login(client)

    # 1. CREAR usuario con imagen JPEG válida
    create_resp = client.post(
        "/users/create",
        data={
            "full_name": "Carlos Mendoza",
            "email": "carlos.mendoza@ejemplo.com",
            "role": "Editor",
            "is_active": "true",
        },
        files={"avatar": ("foto.jpg", io.BytesIO(b"\xff\xd8\xff\xe0fake-jpeg"), "image/jpeg")},
        follow_redirects=False,
    )
    assert create_resp.status_code == 303
    assert create_resp.headers["location"] == "/?msg=created"
    assert s3_mock.put_object.call_count == 1
    put_kwargs = s3_mock.put_object.call_args.kwargs
    assert put_kwargs["Bucket"] == "test-private-avatars-bucket"
    assert put_kwargs["Key"].startswith("avatars/")
    assert put_kwargs["Key"].endswith(".jpg")
    assert put_kwargs["ContentType"] == "image/jpeg"
    first_avatar_key = put_kwargs["Key"]

    # Crear segundo usuario sin foto para probar búsqueda
    client.post(
        "/users/create",
        data={
            "full_name": "Lucía Paredes",
            "email": "lucia.paredes@ejemplo.com",
            "role": "Usuario",
            "is_active": "true",
        },
        follow_redirects=False,
    )

    # 2. LEER y buscar usuarios (incluyendo URL prefirmada S3)
    list_resp = client.get("/")
    assert list_resp.status_code == 200
    assert "Carlos Mendoza" in list_resp.text
    assert "Lucía Paredes" in list_resp.text
    assert f"https://s3.mock.local/test-private-avatars-bucket/{first_avatar_key}" in list_resp.text

    search_resp = client.get("/?q=carlos")
    assert search_resp.status_code == 200
    assert "Carlos Mendoza" in search_resp.text
    assert "Lucía Paredes" not in search_resp.text

    with TestingSessionLocal() as db:
        carlos = db.query(User).filter(User.email == "carlos.mendoza@ejemplo.com").first()
        assert carlos is not None
        carlos_id = carlos.id

    # 3. ACTUALIZAR usuario y reemplazar foto en S3 (eliminando la anterior)
    edit_page = client.get(f"/users/{carlos_id}/edit")
    assert edit_page.status_code == 200
    assert "Carlos Mendoza" in edit_page.text

    update_resp = client.post(
        f"/users/{carlos_id}/edit",
        data={
            "full_name": "Carlos Alberto Mendoza",
            "email": "carlos.alberto@ejemplo.com",
            "role": "Administrador",
            # sin is_active -> cambia a inactivo (False)
        },
        files={"avatar": ("nuevo.png", io.BytesIO(b"\x89PNG\r\n\x1a\nfake-png"), "image/png")},
        follow_redirects=False,
    )
    assert update_resp.status_code == 303
    assert update_resp.headers["location"] == "/?msg=updated"
    assert s3_mock.put_object.call_count == 2
    second_avatar_key = s3_mock.put_object.call_args.kwargs["Key"]
    assert second_avatar_key.endswith(".png")
    assert second_avatar_key != first_avatar_key

    s3_mock.delete_object.assert_called_once_with(
        Bucket="test-private-avatars-bucket",
        Key=first_avatar_key,
    )

    with TestingSessionLocal() as db:
        updated = db.query(User).filter(User.id == carlos_id).first()
        assert updated.full_name == "Carlos Alberto Mendoza"
        assert updated.email == "carlos.alberto@ejemplo.com"
        assert updated.role == "Administrador"
        assert updated.is_active is False
        assert updated.avatar_key == second_avatar_key

    # 4. ELIMINAR usuario en BD y objeto en S3
    s3_mock.delete_object.reset_mock()
    delete_resp = client.post(f"/users/{carlos_id}/delete", follow_redirects=False)
    assert delete_resp.status_code == 303
    assert delete_resp.headers["location"] == "/?msg=deleted"

    s3_mock.delete_object.assert_called_once_with(
        Bucket="test-private-avatars-bucket",
        Key=second_avatar_key,
    )
    with TestingSessionLocal() as db:
        assert db.query(User).filter(User.id == carlos_id).first() is None


def test_file_validation_rejects_invalid_type_and_oversized_files(client: TestClient, mock_s3):
    s3_mock, _ = mock_s3
    _login(client)

    # Tipo MIME no permitido
    bad_type_resp = client.post(
        "/users/create",
        data={
            "full_name": "Usuario Archivo Malo",
            "email": "malo@ejemplo.com",
            "role": "Usuario",
            "is_active": "true",
        },
        files={"avatar": ("notas.txt", io.BytesIO(b"texto plano"), "text/plain")},
    )
    assert bad_type_resp.status_code == 400
    assert "Formato de imagen no válido" in bad_type_resp.text
    assert s3_mock.put_object.call_count == 0

    # Tamaño superior a 5 MB
    oversized_bytes = b"a" * (5 * 1024 * 1024 + 1)
    big_file_resp = client.post(
        "/users/create",
        data={
            "full_name": "Usuario Imagen Pesada",
            "email": "pesada@ejemplo.com",
            "role": "Usuario",
            "is_active": "true",
        },
        files={"avatar": ("grande.webp", io.BytesIO(oversized_bytes), "image/webp")},
    )
    assert big_file_resp.status_code == 400
    assert "5 MB" in big_file_resp.text
    assert s3_mock.put_object.call_count == 0

    # Archivo vacío
    empty_file_resp = client.post(
        "/users/create",
        data={
            "full_name": "Usuario Archivo Vacio",
            "email": "vacio@ejemplo.com",
            "role": "Usuario",
            "is_active": "true",
        },
        files={"avatar": ("vacio.png", io.BytesIO(b""), "image/png")},
    )
    assert empty_file_resp.status_code == 400
    assert "vacío" in empty_file_resp.text
    assert s3_mock.put_object.call_count == 0


def test_duplicate_email_returns_400(client: TestClient):
    _login(client)

    first = client.post(
        "/users/create",
        data={
            "full_name": "Primero",
            "email": "duplicado@ejemplo.com",
            "role": "Usuario",
            "is_active": "true",
        },
        follow_redirects=False,
    )
    assert first.status_code == 303

    second = client.post(
        "/users/create",
        data={
            "full_name": "Segundo",
            "email": "DUPLICADO@ejemplo.com",
            "role": "Usuario",
            "is_active": "true",
        },
    )
    assert second.status_code == 400
    assert "Ya existe un usuario registrado" in second.text


def test_hybrid_s3_client_credentials_selection(monkeypatch, mock_s3):
    _, boto_client_mock = mock_s3

    # Caso 1: Sin credenciales en entorno -> delega a IAM Role de EC2
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "")
    s3_service.get_s3_client()
    kwargs_iam = boto_client_mock.call_args.kwargs
    assert "aws_access_key_id" not in kwargs_iam
    assert "aws_secret_access_key" not in kwargs_iam
    assert kwargs_iam["region_name"] == "us-east-1"

    # Caso 2: Con credenciales explícitas + session token (AWS Academy / local)
    boto_client_mock.reset_mock()
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST123")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "SECRETTEST456")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "TOKENTEST789")
    s3_service.get_s3_client()
    kwargs_keys = boto_client_mock.call_args.kwargs
    assert kwargs_keys["aws_access_key_id"] == "AKIATEST123"
    assert kwargs_keys["aws_secret_access_key"] == "SECRETTEST456"
    assert kwargs_keys["aws_session_token"] == "TOKENTEST789"
