# Plataforma Web de Administración de Usuarios en AWS (EC2 + Docker + RDS + S3)

Aplicación web responsiva y contenerizada desarrollada con **FastAPI**, **Jinja2 + Tailwind CSS**, **SQLAlchemy (PostgreSQL)** y **Amazon S3** para la administración integral de usuarios con almacenamiento privado de fotografías de perfil mediante URLs prefirmadas temporales.

---

## Arquitectura del Sistema

```text
                        +---------------------------------------+
                        |       Administrador (Navegador)       |
                        +-------------------+-------------------+
                                            |
                               HTTP (Puerto 80) / HTTPS
                                            |
                                            v
+---------------------------------------------------------------------------------------+
|                                    AWS VPC                                            |
|                                                                                       |
|  +---------------------------------------------------------------------------------+  |
|  |  Subred Pública - Instancia Amazon EC2 (Ubuntu + Docker)                        |  |
|  |  Security Group: 80/TCP (0.0.0.0/0), 22/TCP (Tu IP pública)                     |  |
|  |  IAM Instance Profile: Acceso S3 de mínimo privilegio sobre bucket privado      |  |
|  |                                                                                 |  |
|  |    +-------------------------------------------------------------------------+  |  |
|  |    | Contenedor Docker (usuario no-root: appuser)                            |  |  |
|  |    | FastAPI + Uvicorn (:8000 <- mapeado desde :80)                          |  |  |
|  |    |  - Sesión firmada con SessionMiddleware + secrets.compare_digest        |  |  |
|  |    |  - Validación estricta de imágenes (JPEG/PNG/WEBP, máx. 5 MB)           |  |  |
|  |    +------------------------+----------------------------+-------------------+  |  |
|  +-----------------------------|----------------------------|----------------------+  |
|                                |                            |                         |
|                  SQLAlchemy    |                            | boto3 (IAM Role o       |
|                (pool_pre_ping) |                            | credenciales temporales)|
|                                v                            |                         |
|  +-----------------------------+---------------+            |                         |
|  | Subred Privada - Amazon RDS (PostgreSQL)    |            |                         |
|  | Security Group: 5432/TCP SOLO desde SG EC2  |            |                         |
|  | Tabla: users (id, full_name, email, role,   |            |                         |
|  |        is_active, avatar_key, created_at)   |            |                         |
|  +---------------------------------------------+            |                         |
+-------------------------------------------------------------|-------------------------+
                                                              v
                                         +----------------------------------------+
                                         | Amazon S3 (Bucket 100% Privado)        |
                                         | Prefijo: avatars/<uuid>.<ext>          |
                                         | Acceso frontend: Presigned URLs (1h)   |
                                         +----------------------------------------+
```

---

## Estructura del Proyecto

```text
.
├── app/
│   ├── __init__.py
│   ├── database.py       # Motor SQLAlchemy (pool_pre_ping=True) y sesión get_db
│   ├── main.py           # Rutas FastAPI, autenticación de administrador y CRUD
│   ├── models.py         # Modelo ORM User
│   └── s3_service.py     # Cliente híbrido boto3, validación de archivos y URLs prefirmadas
├── templates/
│   ├── base.html         # Plantilla base con Tailwind CSS CDN
│   ├── login.html        # Inicio de sesión de administrador
│   ├── dashboard.html    # Panel principal, buscador, métricas y listado
│   └── edit_user.html    # Edición de usuario y reemplazo de fotografía
├── tests/
│   └── test_app.py       # Pruebas automatizadas (pytest + TestClient + mocks boto3)
├── .dockerignore
├── .env.example
├── .gitignore
├── deploy_ec2.sh         # Script automatizado de despliegue en Ubuntu EC2
├── docker-compose.yml    # Entorno local de validación con PostgreSQL 16
├── Dockerfile            # Imagen python:3.11-slim con usuario no-root (appuser)
├── README.md
└── requirements.txt
```

---

## Buenas Prácticas de Seguridad Implementadas

1. **Principio de Mínimo Privilegio en IAM (Instance Profile):**
   - `app/s3_service.py` implementa un cliente híbrido: si `AWS_ACCESS_KEY_ID` y `AWS_SECRET_ACCESS_KEY` se dejan vacías en `.env`, `boto3` delega automáticamente al **IAM Role** adjunto a la instancia EC2, evitando almacenar credenciales estáticas en el servidor.
   - Si trabajas en un entorno con tokens temporales (como **AWS Academy**), el cliente también soporta `AWS_SESSION_TOKEN`.
   - Política IAM recomendada (restringida únicamente al prefijo `avatars/*` del bucket):
     ```json
     {
       "Version": "2012-10-17",
       "Statement": [
         {
           "Sid": "AccesoAvataresS3MinimoPrivilegio",
           "Effect": "Allow",
           "Action": [
             "s3:PutObject",
             "s3:GetObject",
             "s3:DeleteObject"
           ],
           "Resource": "arn:aws:s3:::TU-BUCKET-PRIVADO/avatars/*"
         }
       ]
     }
     ```
2. **Bucket S3 Completamente Privado + URLs Prefirmadas:**
   - El bucket mantiene activado **Block all public access**.
   - El backend genera URLs prefirmadas temporales (`generate_presigned_url`) válidas por 3600 segundos para que el navegador cargue las fotografías sin exponer objetos de forma pública.
3. **Security Groups Restringidos (Defensa en Profundidad):**
   - **EC2 Security Group:**
     - Entrada `80/TCP` desde `0.0.0.0/0` (o `443/TCP` si colocas un Balanceador/HTTPS).
     - Entrada `22/TCP` (SSH) **únicamente desde tu IP pública** (`/32`).
   - **RDS Security Group:**
     - Entrada `5432/TCP` (PostgreSQL) **exclusivamente desde el Security Group de la instancia EC2** (nunca abierto a `0.0.0.0/0`), con acceso público deshabilitado.
4. **Contenedor Docker No-Root:**
   - El `Dockerfile` crea y activa el usuario sin privilegios `appuser`, mitigando el impacto de una eventual vulnerabilidad dentro del contenedor.
5. **Autenticación Segura y Protección de Sesiones:**
   - Validación en tiempo constante mediante `secrets.compare_digest` para evitar ataques de temporización (*timing attacks*).
   - Cookies firmadas criptográficamente con `SessionMiddleware` (`SECRET_KEY`), `same_site="lax"`.
   - Validación estricta de imágenes en servidor (solo `image/jpeg`, `image/png`, `image/webp` y tamaño máximo de 5 MB).

---

## Pruebas Locales y Validación

### 1. Pruebas automatizadas unitarias e integración (`pytest`)
Las pruebas usan SQLite en memoria y mocks de `boto3`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -v
```

### 2. Pruebas locales con Docker Compose (App + PostgreSQL 16)
Para validar el flujo completo contra una instancia real de PostgreSQL antes de subir a AWS:

```bash
cp .env.example .env
# Opcional: configura S3_BUCKET_NAME y credenciales temporales en .env para probar S3 real
docker compose up --build
```

La aplicación estará disponible en `http://localhost:8000` y el endpoint de salud en `http://localhost:8000/health`.

Para detener y limpiar contenedores locales:
```bash
docker compose down -v
```

---

## Guía Paso a Paso de Despliegue en AWS

### Paso 1: Crear el Bucket Privado en Amazon S3
1. Abre la consola de **Amazon S3** -> **Create bucket**.
2. Asigna un nombre único global (por ejemplo, `mi-empresa-users-avatars-2026`) en la misma región que tu EC2 (ej. `us-east-1`).
3. Mantén activado **Block all public access** (todas las casillas marcadas).
4. Habilita cifrado del lado del servidor (**SSE-S3**).
5. Crea el bucket.

### Paso 2: Crear la Base de Datos en Amazon RDS (PostgreSQL)
1. Abre **Amazon RDS** -> **Create database**.
2. Selecciona **PostgreSQL** (plantilla *Free tier* o *Sandbox*).
3. Configura:
   - **DB instance identifier:** `users-admin-db`
   - **Master username:** `postgres`
   - **Master password:** define una contraseña robusta.
   - **Public access:** **No** (solo accesible dentro de la VPC).
   - **Initial database name** (en *Additional configuration*): `users_db`.
4. Crea un Security Group para RDS (`rds-users-sg`) que permita **PostgreSQL (5432)** únicamente desde el Security Group de tu EC2 (`ec2-users-sg`).
5. Una vez en estado **Available**, copia el **Endpoint** (ej. `users-admin-db.xxxx.us-east-1.rds.amazonaws.com`).

### Paso 3: Configurar IAM Role y Lanzar Instancia EC2 (Ubuntu)
1. En **IAM** -> **Roles** -> **Create role**:
   - Tipo de entidad de confianza: **AWS service** -> **EC2**.
   - Adjunta una política inline con permisos `s3:PutObject`, `s3:GetObject` y `s3:DeleteObject` sobre `arn:aws:s3:::TU-BUCKET/avatars/*`.
   - Nombra el rol (ej. `EC2-UsersAdmin-S3Role`).
2. En **EC2** -> **Launch instance**:
   - **AMI:** Ubuntu Server 24.04 LTS o 22.04 LTS.
   - **Instance type:** `t3.micro` o `t2.micro`.
   - **Key pair:** selecciona o crea tu llave `.pem`.
   - **Network settings (Security Group `ec2-users-sg`):**
     - `SSH (22)` -> **My IP**
     - `HTTP (80)` -> **Anywhere (`0.0.0.0/0`)**
   - **Advanced details -> IAM instance profile:** selecciona `EC2-UsersAdmin-S3Role` (si usas AWS Academy y no puedes crear roles, usarás `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` y `AWS_SESSION_TOKEN` en `.env`).

### Paso 4: Conectar por SSH y Desplegar con Docker
```bash
ssh -i tu-llave.pem ubuntu@<IP-PUBLICA-EC2>

git clone <URL-DE-TU-REPOSITORIO>
cd <CARPETA-DEL-PROYECTO>

cp .env.example .env
nano .env
```

Completa tu archivo `.env` en EC2:
```env
DATABASE_URL=postgresql://postgres:TU_PASSWORD_RDS@TU_ENDPOINT_RDS:5432/users_db?sslmode=require
AWS_REGION=us-east-1
S3_BUCKET_NAME=mi-empresa-users-avatars-2026
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_SESSION_TOKEN=
ADMIN_USERNAME=admin
ADMIN_PASSWORD=TuPasswordAdminSegura!
SECRET_KEY=<cadena-aleatoria-generada-con-secrets.token_hex(32)>
```

Ejecuta el script de despliegue:
```bash
chmod +x deploy_ec2.sh
./deploy_ec2.sh
```

Verifica en tu navegador:
- `http://<IP-PUBLICA-EC2>/health` -> `{"status":"ok","service":"admin-users-app"}`
- `http://<IP-PUBLICA-EC2>/login` -> Acceso al panel de administración.

---

## Checklist de Evidencias (Entrega / Laboratorio)

- [ ] **Repositorio y código:** Estructura completa sin archivos `.env` ni credenciales expuestas.
- [ ] **Pruebas unitarias (`pytest`):** Captura de ejecución exitosa con todas las pruebas en verde.
- [ ] **Imagen Docker:** Construcción exitosa (`docker build -t admin-users-app .`) y ejecución con usuario no-root (`appuser`).
- [ ] **Amazon S3 privado:** Captura del bucket con *Block all public access* habilitado y objetos almacenados bajo `avatars/`.
- [ ] **Amazon RDS PostgreSQL:** Captura de instancia *Available*, acceso público deshabilitado y Security Group restringido al SG de EC2.
- [ ] **Amazon EC2 + IAM Role:** Captura de instancia en ejecución, Security Group (`80` público / `22` restringido) y rol IAM adjunto.
- [ ] **Flujo CRUD funcional en la nube:**
  - [ ] Login exitoso con credenciales de administrador.
  - [ ] Creación de usuario con foto de perfil subida a S3 y renderizada mediante URL prefirmada.
  - [ ] Búsqueda por nombre o correo en el dashboard.
  - [ ] Edición de usuario reemplazando la fotografía (verificando eliminación de la anterior en S3).
  - [ ] Eliminación de usuario (verificando borrado en RDS y en S3).

