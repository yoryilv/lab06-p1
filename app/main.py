"""Aplicación principal FastAPI para la administración de usuarios con RDS y S3."""

import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.database import Base, engine, get_db
from app.models import User
from app.s3_service import delete_avatar, get_avatar_url, upload_avatar

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

FLASH_MESSAGES: dict[str, str] = {
    "created": "Usuario creado correctamente.",
    "updated": "Usuario actualizado correctamente.",
    "deleted": "Usuario eliminado correctamente.",
}

ALLOWED_ROLES = ("Administrador", "Editor", "Operador", "Usuario")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializa las tablas en la base de datos al arrancar la aplicación."""
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="Administrador de Usuarios Cloud",
    description="Aplicación web de administración de usuarios con FastAPI, RDS PostgreSQL y Amazon S3.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "dev-secret-key-cambia-en-produccion").strip()
    or "dev-secret-key-cambia-en-produccion",
    session_cookie="admin_session",
    max_age=86400,
    same_site="lax",
    https_only=False,
)


class _NotAuthenticatedError(Exception):
    """Excepción interna para redirigir peticiones sin sesión activa al login."""


@app.exception_handler(_NotAuthenticatedError)
async def _not_authenticated_handler(request: Request, exc: _NotAuthenticatedError):
    return _redirect_to_login()


def _verify_admin_credentials(username: str, password: str) -> bool:
    expected_user = os.getenv("ADMIN_USERNAME", "admin")
    expected_pass = os.getenv("ADMIN_PASSWORD", "admin123")
    valid_user = secrets.compare_digest(username.encode("utf-8"), expected_user.encode("utf-8"))
    valid_pass = secrets.compare_digest(password.encode("utf-8"), expected_pass.encode("utf-8"))
    return valid_user and valid_pass


def _is_authenticated(request: Request) -> bool:
    return bool(request.session.get("authenticated") and request.session.get("admin_user"))


def require_admin(request: Request) -> str:
    """Dependencia que protege rutas administrativas exigiendo sesión iniciada."""
    if not _is_authenticated(request):
        raise _NotAuthenticatedError()
    return str(request.session["admin_user"])


def _redirect_to_login() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


def _serialize_user(user: User) -> dict:
    return {
        "id": user.id,
        "full_name": user.full_name,
        "email": user.email,
        "role": user.role,
        "is_active": user.is_active,
        "avatar_key": user.avatar_key,
        "avatar_url": get_avatar_url(user.avatar_key),
        "created_at": user.created_at,
    }


def _build_dashboard_context(
    request: Request,
    db: Session,
    search_query: str = "",
    message: Optional[str] = None,
    error: Optional[str] = None,
    form_data: Optional[dict] = None,
) -> dict:
    clean_query = search_query.strip()
    query = db.query(User)
    if clean_query:
        pattern = f"%{clean_query}%"
        query = query.filter(
            or_(User.full_name.ilike(pattern), User.email.ilike(pattern))
        )

    users_db = query.order_by(User.created_at.desc(), User.id.desc()).all()
    users = [_serialize_user(u) for u in users_db]

    all_users = db.query(User).all()
    stats = {
        "total": len(all_users),
        "active": sum(1 for u in all_users if u.is_active),
        "inactive": sum(1 for u in all_users if not u.is_active),
        "with_avatar": sum(1 for u in all_users if bool(u.avatar_key)),
    }

    return {
        "admin_user": request.session.get("admin_user"),
        "users": users,
        "stats": stats,
        "q": clean_query,
        "roles": ALLOWED_ROLES,
        "message": message,
        "error": error,
        "form_data": form_data or {"full_name": "", "email": "", "role": "Usuario", "is_active": True},
    }


@app.get("/health", response_class=JSONResponse)
def healthcheck():
    """Endpoint de monitoreo de salud de la aplicación."""
    return {"status": "ok", "service": "admin-users-app"}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    """Muestra el formulario de inicio de sesión para el administrador."""
    if _is_authenticated(request):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None, "username": ""},
    )


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
):
    """Valida credenciales de administrador y crea sesión firmada en cookie."""
    clean_username = username.strip()
    if not _verify_admin_credentials(clean_username, password):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "Credenciales inválidas. Verifica usuario y contraseña.",
                "username": clean_username,
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    request.session.clear()
    request.session["authenticated"] = True
    request.session["admin_user"] = clean_username
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/logout")
def logout(request: Request):
    """Cierra la sesión del administrador."""
    request.session.clear()
    return _redirect_to_login()


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    q: str = "",
    msg: Optional[str] = None,
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Panel principal protegido con búsqueda y listado de usuarios."""
    flash_text = FLASH_MESSAGES.get(msg) if msg else None
    context = _build_dashboard_context(
        request=request,
        db=db,
        search_query=q,
        message=flash_text,
    )
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=context,
    )


@app.post("/users/create", response_class=HTMLResponse)
def create_user(
    request: Request,
    full_name: str = Form(""),
    email: str = Form(""),
    role: str = Form("Usuario"),
    is_active: bool = Form(False),
    avatar: Optional[UploadFile] = File(None),
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Crea un usuario nuevo en base de datos y opcionalmente sube su avatar a S3."""
    clean_name = full_name.strip()
    clean_email = email.strip().lower()
    clean_role = role.strip() if role.strip() in ALLOWED_ROLES else "Usuario"
    form_data = {
        "full_name": clean_name,
        "email": clean_email,
        "role": clean_role,
        "is_active": is_active,
    }

    if not clean_name or not clean_email:
        context = _build_dashboard_context(
            request=request,
            db=db,
            error="El nombre completo y el correo electrónico son obligatorios.",
            form_data=form_data,
        )
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=context,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    existing_user = db.query(User).filter(User.email == clean_email).first()
    if existing_user:
        context = _build_dashboard_context(
            request=request,
            db=db,
            error="Ya existe un usuario registrado con ese correo electrónico.",
            form_data=form_data,
        )
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=context,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    avatar_key: Optional[str] = None
    if avatar and avatar.filename:
        try:
            avatar_key = upload_avatar(avatar)
        except HTTPException as exc:
            context = _build_dashboard_context(
                request=request,
                db=db,
                error=str(exc.detail),
                form_data=form_data,
            )
            return templates.TemplateResponse(
                request=request,
                name="dashboard.html",
                context=context,
                status_code=exc.status_code,
            )

    new_user = User(
        full_name=clean_name,
        email=clean_email,
        role=clean_role,
        is_active=is_active,
        avatar_key=avatar_key,
    )

    try:
        db.add(new_user)
        db.commit()
    except IntegrityError:
        db.rollback()
        if avatar_key:
            delete_avatar(avatar_key)
        context = _build_dashboard_context(
            request=request,
            db=db,
            error="Ya existe un usuario registrado con ese correo electrónico.",
            form_data=form_data,
        )
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=context,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except Exception:
        db.rollback()
        if avatar_key:
            delete_avatar(avatar_key)
        raise

    return RedirectResponse(url="/?msg=created", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/users/{user_id}/edit", response_class=HTMLResponse)
def edit_user_page(
    request: Request,
    user_id: int,
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Muestra el formulario de edición para un usuario específico."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuario no encontrado.",
        )

    return templates.TemplateResponse(
        request=request,
        name="edit_user.html",
        context={
            "admin_user": request.session.get("admin_user"),
            "user": _serialize_user(user),
            "roles": ALLOWED_ROLES,
            "error": None,
        },
    )


@app.post("/users/{user_id}/edit", response_class=HTMLResponse)
def update_user(
    request: Request,
    user_id: int,
    full_name: str = Form(""),
    email: str = Form(""),
    role: str = Form("Usuario"),
    is_active: bool = Form(False),
    remove_avatar: bool = Form(False),
    avatar: Optional[UploadFile] = File(None),
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Actualiza los datos del usuario y reemplaza su imagen en S3 si se carga una nueva."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuario no encontrado.",
        )

    clean_name = full_name.strip()
    clean_email = email.strip().lower()
    clean_role = role.strip() if role.strip() in ALLOWED_ROLES else "Usuario"

    def _render_edit_error(message: str, status_code: int):
        user_view = _serialize_user(user)
        user_view.update(
            {
                "full_name": clean_name,
                "email": clean_email,
                "role": clean_role,
                "is_active": is_active,
            }
        )
        return templates.TemplateResponse(
            request=request,
            name="edit_user.html",
            context={
                "admin_user": request.session.get("admin_user"),
                "user": user_view,
                "roles": ALLOWED_ROLES,
                "error": message,
            },
            status_code=status_code,
        )

    if not clean_name or not clean_email:
        return _render_edit_error(
            "El nombre completo y el correo electrónico son obligatorios.",
            status.HTTP_400_BAD_REQUEST,
        )

    duplicate = (
        db.query(User)
        .filter(User.email == clean_email, User.id != user_id)
        .first()
    )
    if duplicate:
        return _render_edit_error(
            "Ya existe otro usuario registrado con ese correo electrónico.",
            status.HTTP_400_BAD_REQUEST,
        )

    old_avatar_key = user.avatar_key
    new_avatar_key: Optional[str] = None
    should_delete_old = False

    if avatar and avatar.filename:
        try:
            new_avatar_key = upload_avatar(avatar)
            should_delete_old = bool(old_avatar_key)
        except HTTPException as exc:
            return _render_edit_error(str(exc.detail), exc.status_code)
    elif remove_avatar and old_avatar_key:
        should_delete_old = True

    user.full_name = clean_name
    user.email = clean_email
    user.role = clean_role
    user.is_active = is_active
    if new_avatar_key is not None:
        user.avatar_key = new_avatar_key
    elif remove_avatar:
        user.avatar_key = None

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if new_avatar_key:
            delete_avatar(new_avatar_key)
        return _render_edit_error(
            "Ya existe otro usuario registrado con ese correo electrónico.",
            status.HTTP_400_BAD_REQUEST,
        )
    except Exception:
        db.rollback()
        if new_avatar_key:
            delete_avatar(new_avatar_key)
        raise

    if should_delete_old and old_avatar_key:
        delete_avatar(old_avatar_key)

    return RedirectResponse(url="/?msg=updated", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/users/{user_id}/delete")
def delete_user(
    request: Request,
    user_id: int,
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Elimina un usuario en PostgreSQL y borra su imagen asociada en Amazon S3."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuario no encontrado.",
        )

    avatar_key = user.avatar_key
    db.delete(user)
    db.commit()

    if avatar_key:
        delete_avatar(avatar_key)

    return RedirectResponse(url="/?msg=deleted", status_code=status.HTTP_303_SEE_OTHER)
