import os
import re
import json
import time
import io
from uuid import uuid4
from pathlib import Path
from contextlib import contextmanager

import streamlit as st
from PIL import Image, UnidentifiedImageError

from git import Repo, GitCommandError, InvalidGitRepositoryError, NoSuchPathError
from openai import OpenAI


# =========================================================
# UI
# =========================================================
st.set_page_config(page_title="Gestor de Catálogo", layout="centered")
st.title("🛍️ Gestor Automático de Catálogo (Ultra-Robusto)")


# =========================================================
# PATHS
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
JSON_PATH = BASE_DIR / "productos.json"
IMG_DIR = BASE_DIR / "imagenes"
STREAMLIT_DIR = BASE_DIR / ".streamlit"
GITIGNORE_PATH = BASE_DIR / ".gitignore"

def get_lock_path() -> Path:
    """
    Pone el lock fuera del working tree para que Git (stash/pull/rebase)
    no intente moverlo/eliminarlo. Preferimos .git/ porque Git nunca lo stashea.
    Si no existe .git, usamos el directorio temporal del usuario.
    """
    git_dir = BASE_DIR / ".git"
    if git_dir.exists() and git_dir.is_dir():
        return git_dir / "catalogo.lock"

    # Fallback: temp del sistema (por si ejecutas fuera de un repo)
    tmp = Path(os.environ.get("TEMP", str(BASE_DIR)))
    return tmp / "catalogo.lock"

LOCK_PATH = get_lock_path()


IMG_DIR.mkdir(exist_ok=True)
STREAMLIT_DIR.mkdir(exist_ok=True)


# =========================================================
# Helpers: secrets/env (SIN hardcode)
# Streamlit sugiere mantener secretos fuera del repo (secrets.toml, env vars, etc.) [5](https://www.codegenes.net/blog/vscode-please-clean-your-repository-working-tree-before-checkout/)
# =========================================================
def get_secret(key: str, default=None):
    """
    st.secrets puede fallar si no existe secrets.toml. Lo atrapamos para no romper.
    """
    try:
        v = st.secrets.get(key, None)
        if v is not None:
            return v
    except Exception:
        pass
    return os.environ.get(key, default)

# =========================================================
# SSH para Git en Streamlit Cloud (evita: Host key verification failed)
# =========================================================
import tempfile
import stat

def configure_git_ssh():
    """
    Configura GIT_SSH_COMMAND para que Git use una deploy key (privada)
    y un known_hosts pre-cargado (GitHub).

    Secrets esperados en st.secrets:
      - GIT_SSH_PRIVATE_KEY (contenido de la llave privada)
      - GIT_KNOWN_HOSTS (líneas known_hosts oficiales de GitHub)
      - (opcional) GIT_REMOTE_SSH_URL (git@github.com:OWNER/REPO.git)
    """
    priv = get_secret('GIT_SSH_PRIVATE_KEY', '')
    if not str(priv).strip():
        return

    known = get_secret('GIT_KNOWN_HOSTS', '')

    tmpdir = Path(tempfile.gettempdir())
    key_path = tmpdir / 'deploy_key_catalogo'
    key_path.write_text(str(priv).strip() + '\n', encoding='utf-8')
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600

    if str(known).strip():
        known_path = tmpdir / 'known_hosts'
        known_path.write_text(str(known).strip() + '\n', encoding='utf-8')
        known_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        os.environ['GIT_SSH_COMMAND'] = (
            f"ssh -i {key_path} -o IdentitiesOnly=yes "
            f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={known_path}"
        )
    else:
        # Modo rápido (menos estricto): acepta host nuevo automáticamente
        os.environ['GIT_SSH_COMMAND'] = (
            f"ssh -i {key_path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
        )

# Llamar una vez al iniciar
configure_git_ssh()



# =========================================================
# Configuración (GitHub Pages desde branch contenido, root)
# =========================================================
GIT_REMOTE_NAME = get_secret("GIT_REMOTE_NAME", "origin")
GIT_TARGET_BRANCH = get_secret("GIT_TARGET_BRANCH", "contenido")  # ✅ tu branch de Pages
GIT_MAX_RETRIES = int(get_secret("GIT_MAX_RETRIES", 2))
GIT_PULL_BEFORE_PUSH = str(get_secret("GIT_PULL_BEFORE_PUSH", "true")).lower() in ("1", "true", "yes", "y")

# IA preview (opcional)
OPENAI_API_KEY = get_secret("OPENAI_API_KEY", "")


# =========================================================
# Lock simple (evita doble click / reruns simultáneos)
# =========================================================
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

        # En Windows, eliminar puede fallar por locks residuales; no abortamos la app por eso.
        try:
            if lock_path.exists():
                lock_path.unlink()
        except Exception:
            # Si no se puede borrar, lo dejamos: al siguiente intento se sobrescribe/expira por timeout.
            pass


# =========================================================
# JSON robusto (evita JSON vacío/corrupto)
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
    """
    Escritura atómica: escribe a .tmp y luego renombra.
    """
    tmp = JSON_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
    tmp.replace(JSON_PATH)


data = load_data()


# =========================================================
# .gitignore robusto: no subir secrets
# Streamlit docs recomiendan no commitear secrets.toml. [5](https://www.codegenes.net/blog/vscode-please-clean-your-repository-working-tree-before-checkout/)
# =========================================================
def ensure_gitignore():
    must_have = [
        ".streamlit/secrets.toml",
        "secrets.toml",
        "__pycache__/",
        "*.pyc",
        ".DS_Store",
    ]

    if not GITIGNORE_PATH.exists():
        GITIGNORE_PATH.write_text(
            "# Auto-generated\n"
            "# NO subir secretos\n"
            + "\n".join(must_have)
            + "\n",
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
# Imagen: validación + preview seguro
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
    """
    Guarda todas las imágenes en /imagenes con nombres únicos.
    Retorna (main_rel, extras_rel).
    """
    saved = {}  # original_name -> rel_url

    for uf in uploaded_files:
        ok, _, _ = safe_open_image(uf)
        if not ok:
            st.warning(f"⚠️ Saltando '{uf.name}' (imagen inválida).")
            continue

        ext = uf.name.split(".")[-1].lower()
        unique_name = f"prod_{int(time.time())}_{uuid4().hex[:8]}.{ext}"
        abs_path = IMG_DIR / unique_name
        abs_path.write_bytes(uf.getbuffer())
        saved[uf.name] = f"./imagenes/{unique_name}"

    main_rel = saved.get(main_name)
    extras_rel = [url for name, url in saved.items() if name != main_name]
    return main_rel, extras_rel


# =========================================================
# IA "Preview" (opcional, texto-only)
# Nota: es preview, no visión. Solo usa categoría+notas+nombres archivo.
# =========================================================
def ai_preview_generate(category: str, notes: str, filenames: list[str]) -> tuple[str, str]:
    if not OPENAI_API_KEY:
        raise RuntimeError("No hay OPENAI_API_KEY (secrets o env).")

    client = OpenAI(api_key=OPENAI_API_KEY)

    prompt = f"""
Eres un asistente que redacta fichas de productos para un catálogo de regalos artesanales en Ecuador.
Devuelve SOLO JSON válido con claves: title, description.
- title: corto y atractivo (máx 60 caracteres)
- description: 2 líneas (máx 220 caracteres), tono comercial, sin emojis.

Categoría: {category}
Notas: {notes or "Sin notas"}
Archivos (referencia): {", ".join(filenames) if filenames else "N/A"}
"""

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.6,
    )

    text = resp.choices[0].message.content or ""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    m = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if m:
        cleaned = m.group(0)

    obj = json.loads(cleaned)
    return (obj.get("title", "").strip(), obj.get("description", "").strip())


# =========================================================
# Git robusto
# IMPORTANTÍSIMO:
# Git pull --rebase falla si hay cambios sin commitear; se debe commit/stash o usar autostash. [6](https://github.com/LazaUK/AOAI-EntraIDAuth-SDKv1)[7](https://pypi.org/project/openai/)
# Aquí implementamos: stash -u automático + pull --rebase + pop.
# =========================================================
def open_repo_or_fail():
    try:
        return Repo(str(BASE_DIR))
    except (InvalidGitRepositoryError, NoSuchPathError):
        raise RuntimeError("❌ Esta carpeta no es un repositorio Git (falta .git). Clona el repo con git clone.")


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
    # incluye tracked + untracked
    return repo.is_dirty(untracked_files=True)


def git_stash_push(repo: Repo, message="auto before rebase"):
    # -u incluye untracked (imagenes nuevas, etc.)
    repo.git.stash("push", "-u", "-m", message)


def git_stash_pop(repo: Repo):
    repo.git.stash("pop")


def git_pull_rebase_safe(repo: Repo, remote_name: str, branch: str):
    """
    Pull rebase robusto:
    - Si hay cambios locales, stash -u
    - pull --rebase
    - pop stash
    Esto evita: "cannot pull with rebase: You have unstaged changes" [6](https://github.com/LazaUK/AOAI-EntraIDAuth-SDKv1)[7](https://pypi.org/project/openai/)
    """
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
                    "Resuelve conflictos manualmente (git status) y luego commitea. "
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
    """
    Push robusto:
    - Si push falla (ej. non-fast-forward), sincroniza y reintenta.
    """
    remote = repo.remote(name=remote_name)
    last_err = None
    for _ in range(max_tries + 1):
        try:
            remote.push(refspec=f"{branch}:{branch}")
            return
        except Exception as e:
            last_err = e
            # Re-sincroniza y reintenta
            git_pull_rebase_safe(repo, remote_name, branch)

    raise RuntimeError(f"Git push falló tras reintentos: {last_err}")


def git_sync_commit_push(commit_message: str):
    """
    Flujo robusto:
    1) asegurar .gitignore
    2) abrir repo
    3) checkout branch contenido
    4) pull rebase safe (stash -u si hace falta)
    5) commit
    6) push con retry
    """
    ensure_gitignore()

    repo = open_repo_or_fail()
    # Fuerza URL SSH del remote si se proporciona (evita remotes https en Cloud)
    ssh_url = get_secret('GIT_REMOTE_SSH_URL', '')
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
# UI: Subir producto
# =========================================================
st.markdown("### 1) Subir Nuevo Producto (multi-imagen)")

uploaded_files = st.file_uploader(
    "Arrastra las fotos del producto aquí (puedes seleccionar varias)",
    type=["png", "jpg", "jpeg"],
    accept_multiple_files=True,
)

if uploaded_files:
    st.write(f"📸 Imágenes seleccionadas: {len(uploaded_files)}")

    # Preview seguro
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

    # Streamlit: todo form debe tener st.form_submit_button [3](https://learn.microsoft.com/en-us/azure/cost-management-billing/manage/subscription-states)[4](https://stackoverflow.com/questions/67034726/azure-account-free-trial-subscription-disabled-how-to-activate-it-again)
    with st.form("nuevo_producto", clear_on_submit=False):
        col1, col2 = st.columns(2)
        with col1:
            nuevo_titulo = st.text_input("Título del Producto", value=st.session_state.get("title_suggested", ""))
            nuevo_precio = st.number_input("Precio ($)", min_value=0.50, step=0.50)
        with col2:
            if not isinstance(data.get("categories", {}), dict) or not data["categories"]:
                st.error("No hay categorías en productos.json (data['categories']).")
                st.stop()
            nueva_cat = st.selectbox("Categoría", list(data["categories"].keys()))
            nueva_desc = st.text_area("Descripción", value=st.session_state.get("desc_suggested", ""))

        st.markdown("#### IA (Preview - opcional)")
        ai_notes = st.text_input("Notas rápidas para IA (opcional)", value="")
        ai_btn = st.form_submit_button("✨ Generar (Preview IA)")
        save_btn = st.form_submit_button("🚀 Guardar y Publicar (GitHub)")

    # IA preview: rellena sugerencias y re-renderiza
    if ai_btn:
        try:
            title_ai, desc_ai = ai_preview_generate(nueva_cat, ai_notes, file_names)
            st.session_state["title_suggested"] = title_ai
            st.session_state["desc_suggested"] = desc_ai
            st.success("✅ IA (preview) generó sugerencias. Se cargaron en el formulario.")
            st.rerun()
        except Exception as e:
            st.warning(f"⚠️ No se pudo generar con IA (preview): {e}")

    # Guardar + publicar (robusto)
    if save_btn:
        with file_lock(LOCK_PATH, timeout_s=20.0):
            # fallback a sugerencias
            if not nuevo_titulo.strip():
                nuevo_titulo = st.session_state.get("title_suggested", "").strip()
            if not nueva_desc.strip():
                nueva_desc = st.session_state.get("desc_suggested", "").strip()

            if not nuevo_titulo.strip():
                st.error("Falta el título.")
                st.stop()

            # 1) Guardar imágenes
            mainImg, extraImgs = save_images(uploaded_files, main_choice)
            if not mainImg:
                st.error("No se pudo guardar la imagen principal (imagen inválida o error).")
                st.stop()

            # 2) Actualizar JSON
            prod_id = re.sub(r"[^a-zA-Z0-9]", "", nuevo_titulo)[:30] + str(int(time.time()))[-4:]
            nuevo_obj = {
                "title": nuevo_titulo.strip(),
                "price": float(nuevo_precio),
                "description": nueva_desc.strip(),
                "mainImg": mainImg,
                "extraImgs": extraImgs,
            }

            data["products"][prod_id] = nuevo_obj
            data["categories"][nueva_cat].insert(0, prod_id)

            # 3) Guardar JSON atómico
            save_data(data)
            st.success("✅ Producto guardado localmente.")

            # 4) Git: sync + commit + push (stash -u si hace falta)
            # Git usa helpers/SSH del sistema para credenciales (no se guardan en el código) [1](https://platform.openai.com/docs/models/gpt-4o)[2](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/create-resource?view=foundry-classic)
            try:
                msg = git_sync_commit_push(f"Auto: Nuevo producto {nuevo_titulo.strip()}")
                st.balloons()
                st.success("🎉 Catálogo actualizado y subido a GitHub Pages.")
                st.info(msg)
                st.info("Espera unos minutos a que GitHub actualice la página web.")
            except Exception as e:
                st.error(f"Error en Git (pero el producto quedó guardado localmente): {e}")

st.markdown("---")
st.markdown("### Vista previa de datos JSON")
st.json(data)