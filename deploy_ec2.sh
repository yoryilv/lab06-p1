#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="admin-users-app"
CONTAINER_NAME="admin-users-app"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd "$APP_DIR"

echo "=== [1/5] Verificando archivo .env ==="
if [ ! -f ".env" ]; then
    echo "ERROR: No se encontró el archivo .env en $APP_DIR."
    echo "Crea uno a partir de la plantilla: cp .env.example .env && nano .env"
    exit 1
fi

echo "=== [2/5] Verificando instalación de Docker ==="
if ! command -v docker >/dev/null 2>&1; then
    echo "Instalando Docker en Ubuntu..."
    sudo apt-get update -y
    sudo apt-get install -y ca-certificates curl docker.io
    sudo systemctl enable --now docker
    sudo usermod -aG docker "$USER" || true
else
    echo "Docker ya está instalado: $(docker --version)"
    sudo systemctl enable --now docker >/dev/null 2>&1 || true
fi

if docker info >/dev/null 2>&1; then
    DOCKER_CMD="docker"
else
    DOCKER_CMD="sudo docker"
fi

echo "=== [3/5] Construyendo imagen Docker ($IMAGE_NAME) ==="
$DOCKER_CMD build -t "$IMAGE_NAME" .

echo "=== [4/5] Reemplazando contenedor previo (si existe) ==="
if $DOCKER_CMD ps -a --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
    $DOCKER_CMD rm -f "$CONTAINER_NAME"
fi

echo "=== [5/5] Iniciando contenedor en puerto 80:8000 ==="
$DOCKER_CMD run -d \
    --name "$CONTAINER_NAME" \
    -p 80:8000 \
    --env-file .env \
    --restart unless-stopped \
    "$IMAGE_NAME"

echo "Esperando respuesta de /health..."
sleep 3
if command -v curl >/dev/null 2>&1; then
    curl -fsS http://localhost/health && echo ""
fi

echo "Despliegue completado. Contenedor activo:"
$DOCKER_CMD ps --filter "name=$CONTAINER_NAME"

