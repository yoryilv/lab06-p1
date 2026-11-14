"""Servicio híbrido de integración con Amazon S3 para gestión privada de avatares."""

import logging
import os
import uuid
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, UploadFile, status

logger = logging.getLogger(__name__)

ALLOWED_CONTENT_TYPES: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB
DEFAULT_PRESIGNED_EXPIRATION = 3600  # 1 hora


def get_s3_client():
    """Retorna un cliente boto3 S3 híbrido (credenciales explícitas o IAM Role de EC2)."""
    region = os.getenv("AWS_REGION", "us-east-1").strip() or "us-east-1"
    access_key = os.getenv("AWS_ACCESS_KEY_ID", "").strip()
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY", "").strip()
    session_token = os.getenv("AWS_SESSION_TOKEN", "").strip()

    s3_config = Config(signature_version="s3v4")

    if access_key and secret_key:
        client_kwargs = {
            "region_name": region,
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "config": s3_config,
        }
        if session_token:
            client_kwargs["aws_session_token"] = session_token
        return boto3.client("s3", **client_kwargs)

    # Si no hay credenciales explícitas en entorno, delega a la cadena estándar (IAM Role en EC2).
    return boto3.client("s3", region_name=region, config=s3_config)


def _get_bucket_name(required: bool = True) -> str:
    bucket = os.getenv("S3_BUCKET_NAME", "").strip()
    if required and not bucket:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="La variable de entorno S3_BUCKET_NAME no está configurada.",
        )
    return bucket


def validate_image(file: UploadFile) -> bytes:
    """Valida tipo MIME permitido y tamaño máximo de 5 MB para la imagen subida."""
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Formato de imagen no válido. Solo se permiten JPEG, PNG o WEBP.",
        )

    file.file.seek(0)
    content = file.file.read(MAX_FILE_SIZE_BYTES + 1)
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El archivo de imagen está vacío.",
        )

    if len(content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La imagen excede el tamaño máximo permitido de 5 MB.",
        )

    return content


def upload_avatar(file: UploadFile) -> str:
    """Valida y sube una imagen de perfil al prefijo avatars/ en S3, devolviendo su clave."""
    content = validate_image(file)
    extension = ALLOWED_CONTENT_TYPES[file.content_type]
    avatar_key = f"avatars/{uuid.uuid4().hex}{extension}"
    bucket = _get_bucket_name(required=True)

    try:
        client = get_s3_client()
        client.put_object(
            Bucket=bucket,
            Key=avatar_key,
            Body=content,
            ContentType=file.content_type,
        )
        return avatar_key
    except HTTPException:
        raise
    except (BotoCoreError, ClientError) as exc:
        logger.exception("Error subiendo avatar a S3: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo subir la imagen de perfil a Amazon S3.",
        ) from exc


def delete_avatar(avatar_key: Optional[str]) -> bool:
    """Elimina un objeto avatar de S3 si existe la clave y el bucket está configurado."""
    if not avatar_key:
        return False

    bucket = _get_bucket_name(required=False)
    if not bucket:
        return False

    try:
        client = get_s3_client()
        client.delete_object(Bucket=bucket, Key=avatar_key)
        return True
    except (BotoCoreError, ClientError) as exc:
        logger.warning("No se pudo eliminar el objeto '%s' de S3: %s", avatar_key, exc)
        return False


def get_avatar_url(
    avatar_key: Optional[str],
    expiration: int = DEFAULT_PRESIGNED_EXPIRATION,
) -> Optional[str]:
    """Genera una URL prefirmada temporal para acceder de forma segura a un avatar privado."""
    if not avatar_key:
        return None

    bucket = _get_bucket_name(required=False)
    if not bucket:
        return None

    try:
        client = get_s3_client()
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": avatar_key},
            ExpiresIn=expiration,
        )
    except (BotoCoreError, ClientError) as exc:
        logger.warning("No se pudo generar URL prefirmada para '%s': %s", avatar_key, exc)
        return None

