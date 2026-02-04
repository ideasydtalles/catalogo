import os
import re
import json
import time
import io
import stat
import tempfile
import hashlib
from uuid import uuid4
from pathlib import Path
from contextlib import contextmanager

import streamlit as st
from PIL import Image, UnidentifiedImageError
from git import Repo, GitCommandError, InvalidGitRepositoryError, NoSuchPathError

# OpenAI es opcional (solo para “Preview IA”)
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# =========================================================
# UI
# =========================================================
st.set_page_config(page_title="Gestor de Catálogo", layout="centered")

# =========================================================
# PATHS
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
JSON_PATH = BASE_DIR / "productos.json"
IMG_DIR = BASE_DIR / "imagenes"
STREAMLIT_DIR = BASE_DIR / ".streamlit"
GITIGNORE_PATH = BASE_DIR / ".gitignore"

IMG_DIR.mkdir(exist_ok=True)
STREAMLIT_DIR.mkdir(exist_ok=True)

# =========================================================
# Helpers: secrets/env (SIN hardcode)
# =========================================================

def get_secret(key: str, default=None):
    """Lee secretos desde st.secrets (si existe) o env vars."""
    try:
        v = st.secrets.get(key, None)
        if v is not None:
            return v
    except Exception:
        pass
    return os.environ.get(key, default)

# =========================================================
# (Opcional) Login simple (3–4 personas)
# =========================================================
# Activa/desactiva con ENABLE_AUTH=true en Secrets.
ENABLE_AUTH = str(get_secret("ENABLE_AUTH", "false")).lower() in ("1", "true", "yes", "y")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def login_gate():
    """Gate opcional: bloquea app si no se autentica.

    Secrets esperados:
      ENABLE_AUTH = "true"
      [auth]
      salt = "..."
      users = {"usuario":"sha256(salt+password)", ...}

    Para desactivar, no pongas ENABLE_AUTH o déjalo en false.
    """
    if not ENABLE_AUTH:
        return

    try:
        auth = st.secrets.get("auth", {})
    except Exception:
        auth = {}

    users = auth.get("users", {}) if isinstance(auth, dict) else {}
    salt = auth.get("salt", "") if isinstance(auth, dict) else ""

    if not users or not salt:
        st.error("Auth activado pero no configurado. Agrega [auth] en Secrets (salt + users).")
        st.stop()

    # Logout
    if st.session_state.get("auth_ok"):
        with st.sidebar:
            st.success(f"Sesión: {st.session_state.get('auth_user','')}")
            if st.button("Cerrar sesión"):
                st.session_state["auth_ok"] = False
                st.session_state["auth_user"] = ""
                st.rerun()
        return

    st.sidebar.header("🔐 Acceso")
    u = st.sidebar.text_input("Usuario", key="login_user")
    p = st.sidebar.text_input("Contraseña", type="password", key="login_pass")

    attempts = st.session_state.get("login_attempts", 0)
    if attempts >= 10:
        st.sidebar.error("Demasiados intentos. Recarga la página e intenta de nuevo.")
        st.stop()

    if st.sidebar.button("Entrar"):
        expected = users.get(u)
        if expected and _sha256(salt + p) == expected:
            st.session_state["auth_ok"] = True
            st.session_state["auth_user"] = u
            st.session_state["login_attempts"] = 0
            st.rerun()
        else:
            st.session_state["login_attempts"] = attempts + 1
            st.sidebar.error("Credenciales inválidas")

    st.stop()


login_gate()

# =========================================================
# Configuración Git / Pages
# =========================================================
GIT_REMOTE_NAME = get_secret("GIT_REMOTE_NAME", "origin")
GIT_TARGET_BRANCH = get_secret("GIT_TARGET_BRANCH", "contenido")  # branch de Pages
GIT_MAX_RETRIES = int(get_secret("GIT_MAX_RETRIES", 2))
GIT_PULL_BEFORE_PUSH = str(get_secret("GIT_PULL_BEFORE_PUSH", "true")).lower() in ("1", "true", "yes", "y")

# IA preview (opcional)
OPENAI_API_KEY = get_secret("OPENAI_API_KEY", "")

# =========================================================
# SSH para Git en Streamlit Cloud
# =========================================================

def configure_git_ssh():
    """Configura GIT_SSH_COMMAND para usar deploy key y known_hosts.

    Secrets recomendados:
      - GIT_SSH_PRIVATE_KEY: llave privada (texto completo)
      - GIT_KNOWN_HOSTS: líneas known_hosts (github.com ...)
      - GIT_REMOTE_SSH_URL: git@github.com:OWNER/REPO.git (opcional pero recomendado)

    Nota: si GIT_KNOWN_HOSTS no está, se usa accept-new (menos estricto).
    """
    priv = get_secret("GIT_SSH_PRIVATE_KEY", "")
    if not str(priv).strip():
        return

    known = get_secret("GIT_KNOWN_HOSTS", "")

    tmpdir = Path(tempfile.gettempdir())
    key_path = tmpdir / "deploy_key_catalogo"
    key_path.write_text(str(priv).strip() + "\n", encoding="utf-8")
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600

    if str(known).strip():
        known_path = tmpdir / "known_hosts"
        known_path.write_text(str(known).strip() + "\n", encoding="utf-8")
        known_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        os.environ["GIT_SSH_COMMAND"] = (
            f"ssh -i {key_path} -o IdentitiesOnly=yes "
            f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={known_path}"
        )
    else:
        os.environ["GIT_SSH_COMMAND"] = (
            f"ssh -i {key_path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
        )


configure_git_ssh()

# =========================================================
# Lock simple (evita doble click / reruns simultáneos)
# =========================================================

def get_lock_path() -> Path:
    """Pone el lock fuera del working tree para que Git no lo mueva."""
    git_dir = BASE_DIR / ".git"
    if git_dir.exists() and git_dir.is_dir():
        return git_dir / "catalogo.lock"
    tmp = Path(os.environ.get("TEMP", str(BASE_DIR)))
    return tmp / "catalogo.lock"


LOCK_PATH = get_lock_path()


@contextmanager
def file_lock(lock_path: Path, timeout_s: float = 15.0):
    start = time.time()
    fd = None
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            break
        except FileExistsError:
            if (time.time() - start) > timeout_s:
                raise RuntimeError("Otro proceso está ejecutando una operación. Intenta de nuevo.")
            time.sleep(0.2)
    try:
        yield
    finally:
        try:
            if fd is not None:
                os.close(fd)
        except Exception:
            pass
        try:
            if lock_path.exists():
                lock_path.unlink()
        except Exception:
            pass

# =========================================================
# JSON robusto
# =========================================================
DEFAULT_DATA = {"products": {}, "categories": {}}


def load_data():
    if not JSON_PATH.exists():
        save_data(DEFAULT_DATA)
        return json.loads(json.dumps(DEFAULT_DATA))
    raw = JSON_PATH.read_text(encoding="utf-8-sig")
    if not raw.strip():
        return json.loads(json.dumps(DEFAULT_DATA))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        bad = JSON_PATH.with_suffix(".json.bad")
        bad.write_text(raw, encoding="utf-8")
        raise RuntimeError(f"productos.json inválido. Copia guardada en: {bad}")
    data.setdefault("products", {})
    data.setdefault("categories", {})
    return data


def save_data(data):
    tmp = JSON_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
    tmp.replace(JSON_PATH)


data = load_data()

# =========================================================
# .gitignore robusto
# =========================================================

def ensure_gitignore():
    must_have = [
        ".streamlit/secrets.toml",
        "secrets.toml",
        "__pycache__/",
        "*.pyc",
        ".DS_Store",
        # prevención llaves
        ".ssh/",
        "id_rsa",
        "id_rsa.*",
        "*.pem",
        "*.key",
        "*.p12",
    ]
    if not GITIGNORE_PATH.exists():
        GITIGNORE_PATH.write_text(
            "# Auto-generated\n# NO subir secretos\n" + "\n".join(must_have) + "\n",
            encoding="utf-8",
        )
        return
    content = GITIGNORE_PATH.read_text(encoding="utf-8", errors="ignore")
    changed = False
    for line in must_have:
        if line not in content:
            content += ("\n" if not content.endswith("\n") else "") + line + "\n"
            changed = True
    if changed:
        GITIGNORE_PATH.write_text(content, encoding="utf-8")


# =========================================================
# Imagen: validación + guardado
# =========================================================

def safe_open_image(uploaded_file):
    raw = uploaded_file.getvalue()
    if not raw or len(raw) < 16:
        return False, "Archivo vacío o demasiado pequeño", raw
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
        return True, img, raw
    except (UnidentifiedImageError, OSError) as e:
        return False, f"{type(e).__name__}: {e}", raw


def save_images(uploaded_files, main_name: str):
    """Guarda imágenes en /imagenes con nombres únicos.
    Retorna (main_rel, extras_rel).
    """
    saved = {}
    for uf in uploaded_files:
        ok, _, _ = safe_open_image(uf)
        if not ok:
            st.warning(f"⚠️ Saltando '{uf.name}' (imagen inválida).")
            continue
        ext = uf.name.split(".")[-1].lower()
        unique = f"prod_{int(time.time())}_{uuid4().hex[:8]}.{ext}"
        abs_path = IMG_DIR / unique
        abs_path.write_bytes(uf.getbuffer())
        saved[uf.name] = f"./imagenes/{unique}"
    main_rel = saved.get(main_name)
    extras_rel = [url for name, url in saved.items() if name != main_name]
    return main_rel, extras_rel

# =========================================================
# IA preview (opcional)
# =========================================================

def ai_preview_generate(category: str, notes: str, filenames: list[str]) -> tuple[str, str]:
    if OpenAI is None:
        raise RuntimeError("Falta la librería 'openai'. Agrega 'openai' a requirements.txt o desactiva IA.")
    if not OPENAI_API_KEY:
        raise RuntimeError("No hay OPENAI_API_KEY (Secrets o env).")

    client = OpenAI(api_key=OPENAI_API_KEY)
    prompt = f"""
Eres un asistente que redacta fichas de productos para un catálogo de regalos artesanales en Ecuador.
Devuelve SOLO JSON válido con claves: title, description.
- title: corto y atractivo (máx 60 caracteres)
- description: 2 líneas (máx 220 caracteres), tono comercial, sin emojis.
Categoría: {category}
Notas: {notes or 'Sin notas'}
Archivos (referencia): {', '.join(filenames) if filenames else 'N/A'}
"""

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.6,
    )
    text = (resp.choices[0].message.content or "").strip()
    # limpiar fences
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if m:
        text = m.group(0)
    obj = json.loads(text)
    return obj.get("title", "").strip(), obj.get("description", "").strip()

# =========================================================
# Git robusto
# =========================================================

def open_repo_or_fail():
    try:
        return Repo(str(BASE_DIR))
    except (InvalidGitRepositoryError, NoSuchPathError):
        raise RuntimeError("❌ Esta carpeta no es un repositorio Git (falta .git).")


def git_checkout_branch(repo: Repo, branch: str):
    repo.git.fetch(GIT_REMOTE_NAME)
    local_branches = [b.name for b in repo.branches]
    if branch in local_branches:
        repo.git.checkout(branch)
        return
    remote_ref = f"{GIT_REMOTE_NAME}/{branch}"
    all_refs = [r.name for r in repo.refs]
    if remote_ref in all_refs:
        repo.git.checkout("-b", branch, "--track", remote_ref)
    else:
        repo.git.checkout("-b", branch)


def git_has_local_changes(repo: Repo) -> bool:
    return repo.is_dirty(untracked_files=True)


def git_stash_push(repo: Repo, message="auto before rebase"):
    repo.git.stash("push", "-u", "-m", message)


def git_stash_pop(repo: Repo):
    repo.git.stash("pop")


def git_pull_rebase_safe(repo: Repo, remote_name: str, branch: str):
    stashed = False
    try:
        if git_has_local_changes(repo):
            git_stash_push(repo, "auto before rebase")
            stashed = True
        repo.git.pull(remote_name, branch, "--rebase")
        if stashed:
            try:
                git_stash_pop(repo)
            except GitCommandError as e:
                raise RuntimeError(
                    "Pull OK, pero hubo conflicto al re-aplicar el stash. "
                    "Resuelve manualmente (git status) y commitea. "
                    f"Detalle: {e}"
                )
    except GitCommandError as e:
        if stashed:
            try:
                git_stash_pop(repo)
            except Exception:
                pass
        raise RuntimeError(f"Git pull/rebase falló en '{branch}': {e}")


def git_commit(repo: Repo, message: str) -> str:
    repo.git.add(all=True)
    if not repo.is_dirty(untracked_files=True):
        return "No había cambios para commitear."
    repo.index.commit(message)
    return "Commit creado."


def git_push_with_retry(repo: Repo, remote_name: str, branch: str, max_tries: int):
    remote = repo.remote(name=remote_name)
    last_err = None
    for _ in range(max_tries + 1):
        try:
            remote.push(refspec=f"{branch}:{branch}")
            return
        except Exception as e:
            last_err = e
            git_pull_rebase_safe(repo, remote_name, branch)
    raise RuntimeError(f"Git push falló tras reintentos: {last_err}")


def git_sync_commit_push(commit_message: str):
    ensure_gitignore()
    repo = open_repo_or_fail()

    # Fuerza URL SSH del remote si se proporciona (evita remotes https/forks en Cloud)
    ssh_url = get_secret("GIT_REMOTE_SSH_URL", "")
    if ssh_url:
        try:
            repo.remote(name=GIT_REMOTE_NAME).set_url(ssh_url)
        except Exception:
            pass

    git_checkout_branch(repo, GIT_TARGET_BRANCH)

    if GIT_PULL_BEFORE_PUSH:
        git_pull_rebase_safe(repo, GIT_REMOTE_NAME, GIT_TARGET_BRANCH)

    msg_commit = git_commit(repo, commit_message)
    if msg_commit == "No había cambios para commitear.":
        return msg_commit

    git_push_with_retry(repo, GIT_REMOTE_NAME, GIT_TARGET_BRANCH, GIT_MAX_RETRIES)
    return f"Push exitoso a '{GIT_TARGET_BRANCH}'."

# =========================================================
# Helpers de catálogo: edición / eliminación / orden / duplicado
# =========================================================

def product_categories(data_obj, prod_id: str) -> list[str]:
    cats = []
    for k, arr in (data_obj.get("categories", {}) or {}).items():
        if isinstance(arr, list) and prod_id in arr:
            cats.append(k)
    return cats


def remove_product_from_all_categories(data_obj, prod_id: str):
    for k, arr in (data_obj.get("categories", {}) or {}).items():
        if isinstance(arr, list):
            data_obj["categories"][k] = [x for x in arr if x != prod_id]


def ensure_in_category(data_obj, prod_id: str, cat: str, position: int = 0):
    data_obj.setdefault("categories", {})
    data_obj["categories"].setdefault(cat, [])
    arr = data_obj["categories"][cat]
    if prod_id in arr:
        arr = [x for x in arr if x != prod_id]
    position = max(0, min(position, len(arr)))
    arr.insert(position, prod_id)
    data_obj["categories"][cat] = arr


def is_local_repo_image(path: str) -> bool:
    if not path:
        return False
    p = str(path).strip()
    return p.startswith("./imagenes/") or p.startswith("imagenes/")


def image_used_elsewhere(data_obj, img_path: str, excluding_prod_id: str) -> bool:
    """Evita borrar una imagen local que aún es usada por otro producto."""
    if not is_local_repo_image(img_path):
        return False
    for pid, p in (data_obj.get("products", {}) or {}).items():
        if pid == excluding_prod_id:
            continue
        if p.get("mainImg") == img_path:
            return True
        if img_path in (p.get("extraImgs", []) or []):
            return True
    return False


def delete_local_images_if_unused(data_obj, prod_id: str, paths: list[str]):
    deleted, skipped = [], []
    for p in paths:
        if not is_local_repo_image(p):
            continue
        if image_used_elsewhere(data_obj, p, excluding_prod_id=prod_id):
            skipped.append(p)
            continue
        rel = p.replace("./", "")
        abs_path = BASE_DIR / rel
        try:
            if abs_path.exists() and abs_path.is_file():
                abs_path.unlink()
                deleted.append(p)
        except Exception:
            skipped.append(p)
    return deleted, skipped


def move_in_category(data_obj, cat: str, prod_id: str, direction: int) -> bool:
    """Mueve un producto dentro de la lista de una categoría.
    direction = -1 (subir), +1 (bajar)
    """
    arr = data_obj.get("categories", {}).get(cat, []) or []
    if prod_id not in arr:
        return False
    i = arr.index(prod_id)
    j = i + direction
    if j < 0 or j >= len(arr):
        return False
    arr[i], arr[j] = arr[j], arr[i]
    data_obj["categories"][cat] = arr
    return True


def move_to_position(data_obj, cat: str, prod_id: str, new_index: int) -> bool:
    """Mueve prod_id a una posición específica dentro de una categoría."""
    arr = data_obj.get("categories", {}).get(cat, []) or []
    if prod_id not in arr:
        return False
    arr = [x for x in arr if x != prod_id]
    new_index = max(0, min(int(new_index), len(arr)))
    arr.insert(new_index, prod_id)
    data_obj["categories"][cat] = arr
    return True


def generate_unique_product_id(data_obj, title: str) -> str:
    """Genera un ID único y estable (no editable) para un producto nuevo o duplicado."""
    base = re.sub(r"[^a-zA-Z0-9]", "", title)[:26]
    suffix = str(int(time.time()))[-4:]
    candidate = f"{base}{suffix}"
    k = 0
    while candidate in (data_obj.get("products", {}) or {}):
        k += 1
        candidate = f"{base}{suffix}{k}"
        if k > 50:
            candidate = f"{base}{suffix}{int(time.time())}"
            break
    return candidate


# =========================================================
# UI principal (Tabs)
# =========================================================
st.title("🛍️ Gestor Automático de Catálogo")

TAB_ADD, TAB_MANAGE, TAB_JSON = st.tabs(["➕ Subir nuevo producto", "🛠️ Administrar productos", "🧾 Ver JSON"])

# ---------------------------------------------------------
# TAB 1: Subir nuevo producto
# ---------------------------------------------------------
with TAB_ADD:
    st.markdown("### 1) Subir Nuevo Producto (multi-imagen)")

    uploaded_files = st.file_uploader(
        "Arrastra las fotos del producto aquí (puedes seleccionar varias)",
        type=["png", "jpg", "jpeg"],
        accept_multiple_files=True,
        key="upl_new",
    )

    if uploaded_files:
        st.write(f"📸 Imágenes seleccionadas: {len(uploaded_files)}")

        cols = st.columns(min(4, len(uploaded_files)))
        for i, uf in enumerate(uploaded_files):
            ok, img_or_err, _ = safe_open_image(uf)
            with cols[i % len(cols)]:
                if ok:
                    st.image(img_or_err, use_container_width=True)
                else:
                    st.error("No se pudo leer")
                st.caption(uf.name)

        file_names = [uf.name for uf in uploaded_files]
        main_choice = st.selectbox("Elige la imagen PRINCIPAL (mainImg)", options=file_names, index=0)

        st.markdown("---")
        st.markdown("### 2) Datos del producto")

        if not isinstance(data.get("categories", {}), dict) or not data["categories"]:
            st.error("No hay categorías en productos.json (data['categories']).")
            st.stop()

        with st.form("nuevo_producto", clear_on_submit=False):
            col1, col2 = st.columns(2)
            with col1:
                nuevo_titulo = st.text_input("Título del Producto", value=st.session_state.get("title_suggested", ""))
                nuevo_precio = st.number_input("Precio ($)", min_value=0.50, step=0.50)
            with col2:
                nueva_cat = st.selectbox("Categoría", list(data["categories"].keys()))
                nueva_desc = st.text_area("Descripción", value=st.session_state.get("desc_suggested", ""))

            st.markdown("#### IA (Preview - opcional)")
            ai_notes = st.text_input("Notas rápidas para IA (opcional)", value="")
            ai_btn = st.form_submit_button("✨ Generar (Preview IA)")
            save_btn = st.form_submit_button("🚀 Guardar y Publicar")

            if ai_btn:
                try:
                    title_ai, desc_ai = ai_preview_generate(nueva_cat, ai_notes, file_names)
                    st.session_state["title_suggested"] = title_ai
                    st.session_state["desc_suggested"] = desc_ai
                    st.success("✅ IA generó sugerencias. Se cargaron en el formulario.")
                    st.rerun()
                except Exception as e:
                    st.warning(f"⚠️ No se pudo generar con IA: {e}")

            if save_btn:
                with file_lock(LOCK_PATH, timeout_s=20.0):
                    if not nuevo_titulo.strip():
                        nuevo_titulo = st.session_state.get("title_suggested", "").strip()
                    if not nueva_desc.strip():
                        nueva_desc = st.session_state.get("desc_suggested", "").strip()
                    if not nuevo_titulo.strip():
                        st.error("Falta el título.")
                        st.stop()

                    mainImg, extraImgs = save_images(uploaded_files, main_choice)
                    if not mainImg:
                        st.error("No se pudo guardar la imagen principal.")
                        st.stop()

                    prod_id = generate_unique_product_id(data, nuevo_titulo)

                    nuevo_obj = {
                        "title": nuevo_titulo.strip(),
                        "price": float(nuevo_precio),
                        "description": nueva_desc.strip(),
                        "mainImg": mainImg,
                        "extraImgs": extraImgs,
                    }

                    data["products"][prod_id] = nuevo_obj

                    remove_product_from_all_categories(data, prod_id)
                    ensure_in_category(data, prod_id, nueva_cat, position=0)

                    save_data(data)
                    st.success("✅ Producto guardado localmente.")

                    try:
                        msg = git_sync_commit_push(f"Auto: Nuevo producto {nuevo_titulo.strip()}")
                        st.balloons()
                        st.success("🎉 Catálogo actualizado y subido.")
                        st.info(msg)
                        st.info("Espera unos minutos a que GitHub Pages actualice la página.")
                    except Exception as e:
                        st.error(f"Error en Git (pero el producto quedó guardado localmente): {e}")

# ---------------------------------------------------------
# TAB 2: Administrar productos (reordenar / duplicar / editar / eliminar)
# ---------------------------------------------------------
with TAB_MANAGE:
    st.markdown("### Administrar Productos")
    st.caption("El ID NO se modifica. Puedes reordenar, duplicar, editar y eliminar. Al eliminar, se borran imágenes locales del repo si NO están usadas por otro producto.")

    products = data.get("products", {}) or {}
    categories = data.get("categories", {}) or {}
    cat_list = list(categories.keys())

    # ---------- Reordenar ----------
    st.markdown("---")
    st.markdown("#### 🔃 Reordenar productos por categoría")
    st.caption("El orden en categories define el orden de salida en el catálogo.")

    if not cat_list:
        st.warning("No hay categorías definidas.")
    else:
        cat_reorder = st.selectbox("Categoría a reordenar", options=cat_list, key="cat_reorder")
        arr = categories.get(cat_reorder, []) or []
        if not arr:
            st.info("Esa categoría está vacía.")
        else:
            ordered_labels = []
            for i, pid0 in enumerate(arr):
                title0 = (products.get(pid0, {}) or {}).get("title", pid0)
                ordered_labels.append(f"{i+1:02d}. {pid0} — {title0}")

            sel_move = st.selectbox("Producto a mover", options=ordered_labels, key="sel_move_prod")
            sel_pid = sel_move.split(". ", 1)[1].split(" — ", 1)[0]

            cA, cB, cC, cD = st.columns(4)
            with cA:
                up_btn = st.button("⬆️ Subir", key="btn_up")
            with cB:
                down_btn = st.button("⬇️ Bajar", key="btn_down")
            with cC:
                top_btn = st.button("⤒ Al inicio", key="btn_top")
            with cD:
                bottom_btn = st.button("⤓ Al final", key="btn_bottom")

            pos_col1, pos_col2 = st.columns([1, 1])
            with pos_col1:
                new_pos = st.number_input(
                    "Mover a posición (1..N)",
                    min_value=1,
                    max_value=len(arr),
                    value=min(arr.index(sel_pid) + 1, len(arr)),
                    key="move_pos",
                )
            with pos_col2:
                go_btn = st.button("Mover", key="btn_move_pos")

            if up_btn or down_btn or top_btn or bottom_btn or go_btn:
                with file_lock(LOCK_PATH, timeout_s=20.0):
                    if up_btn:
                        moved = move_in_category(data, cat_reorder, sel_pid, -1)
                    elif down_btn:
                        moved = move_in_category(data, cat_reorder, sel_pid, +1)
                    elif top_btn:
                        moved = move_to_position(data, cat_reorder, sel_pid, 0)
                    elif bottom_btn:
                        moved = move_to_position(data, cat_reorder, sel_pid, 10**9)
                    else:
                        moved = move_to_position(data, cat_reorder, sel_pid, int(new_pos) - 1)

                    if moved:
                        save_data(data)
                        try:
                            msg = git_sync_commit_push(f"Auto: Reordenar {cat_reorder} ({sel_pid})")
                            st.success("✅ Orden actualizado y publicado.")
                            st.info(msg)
                        except Exception as e:
                            st.error(f"Error en Git (pero el orden quedó guardado localmente): {e}")
                    else:
                        st.warning("No se pudo mover (ya estaba en el borde o no se encontró).")

                st.rerun()

    # ---------- Gestión por producto ----------
    st.markdown("---")
    st.markdown("#### 🧩 Seleccionar producto")

    if not products:
        st.info("No hay productos cargados.")
    else:
        c1, c2 = st.columns([2, 1])
        with c1:
            q = st.text_input("Buscar (título o descripción)", value="", key="q_manage")
        with c2:
            cat_filter = st.selectbox("Filtrar categoría", ["(todas)"] + cat_list, key="cat_manage")

        def _in_cat(pid: str, cat: str) -> bool:
            return pid in (categories.get(cat, []) or [])

        def _match(pid: str, p: dict) -> bool:
            txt = f"{p.get('title','')} {p.get('description','')}".lower()
            ok_q = (q.strip().lower() in txt) if q.strip() else True
            ok_cat = True
            if cat_filter != "(todas)":
                ok_cat = _in_cat(pid, cat_filter)
            return ok_q and ok_cat

        options = [(pid, p.get("title", "(sin título)")) for pid, p in products.items() if _match(pid, p)]
        options.sort(key=lambda x: x[1].lower())

        if not options:
            st.warning("No hay coincidencias con esos filtros.")
        else:
            label_map = {f"{pid} — {title}": pid for pid, title in options}
            selected_label = st.selectbox("Selecciona un producto", list(label_map.keys()), key="sel_prod")
            pid = label_map[selected_label]
            p = products[pid]

            current_cats = product_categories(data, pid)
            default_cat = current_cats[0] if current_cats else (cat_list[0] if cat_list else "")

            # resumen
            left, right = st.columns([1, 2])
            with left:
                if p.get("mainImg"):
                    st.image(p["mainImg"], use_container_width=True)
                st.caption(f"ID: {pid}")
                st.caption(f"Categorías: {', '.join(current_cats) or '(ninguna)'}")
            with right:
                st.write(f"**{p.get('title','')}**")
                st.write(p.get("description", ""))
                st.write(f"Precio: **${float(p.get('price',0) or 0):.2f}**")
                extras = p.get("extraImgs", []) or []
                if extras:
                    st.write("Extras:")
                    st.image(extras[:6], width=120)

            # ---------- Duplicar ----------
            st.markdown("---")
            st.markdown("#### 📄 Duplicar")
            st.caption("Crea una copia con nuevo ID. Reutiliza imágenes (evita duplicación de archivos).")

            dup_title_suffix = st.text_input("Sufijo para el título de la copia", value=" (Copia)", key=f"dup_suf_{pid}")
            dup_to_cat = st.selectbox(
                "Categoría destino de la copia",
                options=cat_list,
                index=cat_list.index(default_cat) if default_cat in cat_list else 0,
                key=f"dup_cat_{pid}",
            )
            if st.button("📄 Duplicar producto", key=f"dup_btn_{pid}"):
                with file_lock(LOCK_PATH, timeout_s=20.0):
                    src_prod = data["products"].get(pid, {})
                    new_id = generate_unique_product_id(data, (src_prod.get("title", "Producto") + "Copia"))

                    copy_obj = {
                        "title": (src_prod.get("title", "") + dup_title_suffix).strip(),
                        "price": float(src_prod.get("price", 0.0) or 0.0),
                        "description": (src_prod.get("description", "") or "").strip(),
                        "mainImg": src_prod.get("mainImg", ""),
                        "extraImgs": list(src_prod.get("extraImgs", []) or []),
                    }

                    data["products"][new_id] = copy_obj
                    remove_product_from_all_categories(data, new_id)
                    ensure_in_category(data, new_id, dup_to_cat, position=0)
                    save_data(data)

                    try:
                        msg = git_sync_commit_push(f"Auto: Duplicar producto {pid} -> {new_id}")
                        st.success(f"✅ Duplicado creado: {new_id}")
                        st.info(msg)
                    except Exception as e:
                        st.error(f"Error en Git (pero el duplicado quedó guardado localmente): {e}")

                st.rerun()

            # ---------- Editar ----------
            st.markdown("---")
            st.markdown("#### ✏️ Editar")

            with st.form(f"edit_{pid}", clear_on_submit=False):
                col1, col2 = st.columns(2)
                with col1:
                    new_title = st.text_input("Título", value=p.get("title", ""))
                    new_price = st.number_input("Precio ($)", min_value=0.0, step=0.5, value=float(p.get("price", 0.0) or 0.0))
                with col2:
                    new_cat = st.selectbox("Categoría", options=cat_list, index=cat_list.index(default_cat) if default_cat in cat_list else 0)
                    new_desc = st.text_area("Descripción", value=p.get("description", ""))

                st.markdown("##### Imágenes")
                extras = p.get("extraImgs", []) or []

                main_option = st.radio(
                    "Acción para imagen principal",
                    options=["Mantener principal", "Reemplazar principal con nueva imagen", "Usar una imagen extra existente como principal"],
                    index=0,
                )

                new_imgs = st.file_uploader(
                    "Subir nuevas imágenes (opcional)",
                    type=["png", "jpg", "jpeg"],
                    accept_multiple_files=True,
                    key=f"upl_edit_{pid}",
                )

                chosen_new_main = None
                if main_option == "Reemplazar principal con nueva imagen" and new_imgs:
                    chosen_new_main = st.selectbox("Elige cuál de las nuevas será la principal", [uf.name for uf in new_imgs], key=f"choose_main_{pid}")

                chosen_existing_extra_as_main = None
                if main_option == "Usar una imagen extra existente como principal" and extras:
                    chosen_existing_extra_as_main = st.selectbox("Elige la imagen extra que será principal", extras, key=f"choose_extra_main_{pid}")

                remove_extras = st.multiselect("Quitar imágenes extra (selecciona)", options=extras, default=[], key=f"rm_extra_{pid}")

                save_btn = st.form_submit_button("💾 Guardar cambios")

            if save_btn:
                with file_lock(LOCK_PATH, timeout_s=20.0):
                    p2 = data["products"][pid]
                    old_main = p2.get("mainImg", "")

                    # campos
                    p2["title"] = new_title.strip()
                    p2["price"] = float(new_price)
                    p2["description"] = new_desc.strip()

                    # categoría única
                    remove_product_from_all_categories(data, pid)
                    ensure_in_category(data, pid, new_cat, position=0)

                    # quitar extras
                    if remove_extras:
                        p2["extraImgs"] = [x for x in (p2.get("extraImgs", []) or []) if x not in set(remove_extras)]
                        delete_local_images_if_unused(data, pid, remove_extras)

                    # subir nuevas
                    new_main_path = None
                    new_extra_paths = []
                    if new_imgs:
                        if chosen_new_main:
                            main_rel, extra_rel = save_images(new_imgs, chosen_new_main)
                        else:
                            main_rel, extra_rel = save_images(new_imgs, new_imgs[0].name)

                        if chosen_new_main and main_rel:
                            new_main_path = main_rel
                            new_extra_paths = extra_rel
                        else:
                            new_extra_paths = ([main_rel] if main_rel else []) + extra_rel

                        p2["extraImgs"] = (p2.get("extraImgs", []) or []) + new_extra_paths

                    # acción sobre main
                    if main_option == "Reemplazar principal con nueva imagen":
                        if not new_imgs or not chosen_new_main:
                            st.error("Para reemplazar principal debes subir imágenes y escoger una como principal.")
                        else:
                            if new_main_path:
                                p2["mainImg"] = new_main_path
                                delete_local_images_if_unused(data, pid, [old_main])

                    elif main_option == "Usar una imagen extra existente como principal":
                        if chosen_existing_extra_as_main:
                            p2_main = p2.get("mainImg", "")
                            p2["mainImg"] = chosen_existing_extra_as_main
                            p2["extraImgs"] = [x for x in (p2.get("extraImgs", []) or []) if x != chosen_existing_extra_as_main]
                            if p2_main:
                                p2["extraImgs"] = [p2_main] + (p2.get("extraImgs", []) or [])

                    data["products"][pid] = p2
                    save_data(data)

                    try:
                        msg = git_sync_commit_push(f"Auto: Editar producto {pid}")
                        st.success("✅ Cambios guardados y publicados.")
                        st.info(msg)
                    except Exception as e:
                        st.error(f"Error en Git (pero cambios guardados localmente): {e}")

                st.rerun()

            # ---------- Eliminar ----------
            st.markdown("---")
            st.markdown("#### 🗑️ Eliminar")
            st.error("⚠️ Eliminará el producto del catálogo. Se borrarán imágenes locales del repo que NO estén usadas por otros productos.")
            confirm = st.checkbox("Confirmo que quiero eliminar este producto", value=False, key=f"conf_del_{pid}")

            if st.button("🗑️ Eliminar producto", disabled=not confirm, key=f"btn_del_{pid}"):
                with file_lock(LOCK_PATH, timeout_s=20.0):
                    pdel = data["products"].get(pid, {})
                    paths = []
                    if pdel.get("mainImg"):
                        paths.append(pdel.get("mainImg"))
                    paths.extend(pdel.get("extraImgs", []) or [])

                    data["products"].pop(pid, None)
                    remove_product_from_all_categories(data, pid)

                    deleted, skipped = delete_local_images_if_unused(data, pid, paths)

                    save_data(data)

                    try:
                        msg = git_sync_commit_push(f"Auto: Eliminar producto {pid}")
                        st.success("✅ Producto eliminado y publicado.")
                        if deleted:
                            st.info(f"🧹 Imágenes eliminadas: {len(deleted)}")
                        if skipped:
                            st.warning(f"Algunas imágenes no se borraron (se usan en otros productos o no eran locales): {len(skipped)}")
                        st.info(msg)
                    except Exception as e:
                        st.error(f"Error en Git (pero el producto quedó eliminado localmente): {e}")

                st.rerun()

# ---------------------------------------------------------
# TAB 3: Vista previa JSON
# ---------------------------------------------------------
with TAB_JSON:
    st.markdown("### Vista previa de datos JSON")
    st.json(data)

