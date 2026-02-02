import os
import json
import time
import re
from uuid import uuid4
from pathlib import Path

import streamlit as st
from git import Repo, GitCommandError

# OpenAI (preview opcional)
from openai import OpenAI

# ---------------------------
# CONFIG / UI
# ---------------------------
st.set_page_config(page_title="Gestor de Catálogo", layout="centered")
st.title("🛍️ Gestor Automático de Catálogo")

# ---------------------------
# PATHS ROBUSTOS
# ---------------------------
BASE_DIR = Path(__file__).resolve().parent
JSON_PATH = BASE_DIR / "productos.json"
IMG_DIR = BASE_DIR / "imagenes"
IMG_DIR.mkdir(exist_ok=True)

DEFAULT_DATA = {"products": {}, "categories": {}}

# ---------------------------
# SECRETS / ENV (SIN HARDCODE)
# ---------------------------
def get_secret(key: str, default=None):
    """
    Lee desde st.secrets (si existe) o desde variables de entorno.
    Evita caerse si no hay secrets.toml.
    """
    try:
        # st.secrets puede lanzar error si no existe secrets.toml
        v = st.secrets.get(key, None)
        if v is not None:
            return v
    except Exception:
        pass
    return os.environ.get(key, default)

OPENAI_API_KEY = get_secret("OPENAI_API_KEY")  # opcional
# Para git
GIT_REMOTE_NAME = get_secret("GIT_REMOTE_NAME", "origin")
GIT_TARGET_BRANCH = get_secret("GIT_TARGET_BRANCH", "")  # si vacío: usa el branch actual
GIT_PULL_BEFORE_PUSH = str(get_secret("GIT_PULL_BEFORE_PUSH", "true")).lower() in ("1", "true", "yes", "y")

# ---------------------------
# JSON IO ROBUSTO
# ---------------------------
def load_data():
    if not JSON_PATH.exists():
        JSON_PATH.write_text(json.dumps(DEFAULT_DATA, ensure_ascii=False, indent=2), encoding="utf-8")
        return json.loads(json.dumps(DEFAULT_DATA))

    raw = JSON_PATH.read_text(encoding="utf-8-sig")
    if not raw.strip():
        return json.loads(json.dumps(DEFAULT_DATA))

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        bad = JSON_PATH.with_suffix(".json.bad")
        bad.write_text(raw, encoding="utf-8")
        raise RuntimeError(
            f"productos.json inválido. Guardé una copia en: {bad}"
        )

def save_data(data):
    JSON_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")

data = load_data()

# Asegurar estructura mínima
data.setdefault("products", {})
data.setdefault("categories", {})

# ---------------------------
# UTIL: crear ID de producto
# ---------------------------
def make_product_id(title: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9]", "", title)[:30] or "Producto"
    return f"{base}{str(int(time.time()))[-4:]}"

# ---------------------------
# UTIL: guardar imágenes (main + extras)
# ---------------------------
def save_images(uploaded_files, main_name: str):
    """
    Guarda todas las imágenes en /imagenes con nombres únicos.
    Retorna (main_rel, extras_rel)
    """
    saved = {}  # original_name -> rel_url
    for uf in uploaded_files:
        ext = uf.name.split(".")[-1].lower()
        unique_name = f"prod_{int(time.time())}_{uuid4().hex[:8]}.{ext}"
        abs_path = IMG_DIR / unique_name
        abs_path.write_bytes(uf.getbuffer())
        saved[uf.name] = f"./imagenes/{unique_name}"

    main_rel = saved.get(main_name)
    extras_rel = [url for name, url in saved.items() if name != main_name]
    return main_rel, extras_rel

# ---------------------------
# IA PREVIEW (OPCIONAL)
# ---------------------------
def ai_preview_generate(category: str, notes: str, filenames: list[str]) -> tuple[str, str]:
    """
    IA en modo 'preview': genera title + description SOLO con texto (sin visión),
    usando notas y nombres de archivos como contexto.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("No hay OPENAI_API_KEY configurada (secrets o env).")

    client = OpenAI(api_key=OPENAI_API_KEY)

    prompt = f"""
Eres un asistente que redacta fichas de productos para un catálogo de regalos artesanales en Ecuador.
Devuelve SOLO JSON válido con claves: title, description.
- title: corto y atractivo (máx 60 caracteres)
- description: 2 líneas (máx 220 caracteres), tono comercial, sin emojis.

Categoría: {category}
Notas del producto: {notes or "No hay notas adicionales."}
Nombres de archivos (referencia): {", ".join(filenames) if filenames else "N/A"}
"""

    # Modo preview: usamos chat completions y pedimos JSON.
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.6,
    )
    text = resp.choices[0].message.content or ""

    # Extraer JSON del texto (por si viene con fences)
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    m = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if m:
        cleaned = m.group(0)

    obj = json.loads(cleaned)
    return (obj.get("title", "").strip(), obj.get("description", "").strip())

# ---------------------------
# GIT: pull/rebase + commit + push
# ---------------------------
def git_sync_commit_push(commit_message: str):
    """
    - Abre repo
    - Cambia al branch objetivo (contenido)
    - Si no existe localmente, lo crea desde origin/contenido
    - fetch + pull --rebase (opcional)
    - add/commit/push
    """
    repo = Repo(str(BASE_DIR))

    # Branch actual
    try:
        current_branch = repo.active_branch.name
    except TypeError:
        current_branch = "main"

    target_branch = (GIT_TARGET_BRANCH.strip() or current_branch)

    # Asegurar que exista el remoto
    try:
        remote = repo.remote(name=GIT_REMOTE_NAME)
    except Exception as e:
        raise RuntimeError(f"No existe el remoto '{GIT_REMOTE_NAME}'. Error: {e}")

    # Fetch siempre (para conocer origin/target_branch)
    repo.git.fetch(GIT_REMOTE_NAME)

    # Si el branch destino no existe localmente, intentamos crearlo desde origin
    local_branches = [b.name for b in repo.branches]
    if target_branch not in local_branches:
        # crear desde origin/target_branch si existe
        remote_ref = f"{GIT_REMOTE_NAME}/{target_branch}"
        remote_refs = [r.name for r in repo.refs]
        if remote_ref in remote_refs:
            repo.git.checkout("-b", target_branch, "--track", remote_ref)
        else:
            # si no existe remoto, lo creamos vacío desde el branch actual
            repo.git.checkout("-b", target_branch)
    else:
        # si existe, solo checkout
        if current_branch != target_branch:
            repo.git.checkout(target_branch)

    # Pull antes de push (rebase para evitar non-fast-forward)
    if GIT_PULL_BEFORE_PUSH:
        try:
            repo.git.pull(GIT_REMOTE_NAME, target_branch, "--rebase")
        except GitCommandError as e:
            raise RuntimeError(f"Git pull/rebase falló en '{target_branch}': {e}")

    # Add + commit
    repo.git.add(all=True)

    if repo.is_dirty(untracked_files=True):
        repo.index.commit(commit_message)
    else:
        return "No había cambios para commitear."

    # Push al branch de Pages (contenido)
    try:
        remote.push(refspec=f"{target_branch}:{target_branch}")
        return f"Push exitoso al branch '{target_branch}'."
    except Exception as e:
        raise RuntimeError(f"Git push falló: {e}")
    
# ---------------------------
# UI PRINCIPAL
# ---------------------------
st.markdown("### 1) Subir Nuevo Producto")

# Uploader múltiple
uploaded_files = st.file_uploader(
    "Arrastra las fotos del producto aquí (puedes seleccionar varias)",
    type=["png", "jpg", "jpeg"],
    accept_multiple_files=True
)

if uploaded_files:
    st.write(f"📸 Imágenes seleccionadas: {len(uploaded_files)}")

    # Preview fuera del form (para no depender de submit)
    cols = st.columns(min(4, len(uploaded_files)))
    for i, uf in enumerate(uploaded_files):
        with cols[i % len(cols)]:
            st.image(uf, use_container_width=True)
            st.caption(uf.name)

    # Selector de principal
    file_names = [uf.name for uf in uploaded_files]
    main_choice = st.selectbox("Elige la imagen PRINCIPAL (mainImg)", options=file_names, index=0)

    st.markdown("---")
    st.markdown("### 2) Datos del producto")

    # FORM: OJO, siempre debe existir st.form_submit_button dentro del form. [1](https://scitechlabuio-my.sharepoint.com/personal/cristian_ganan_scitech-lab_com/Documents/Archivos%20de%20Microsoft%C2%A0Copilot%20Chat/index.html)[2](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/reference?view=foundry-classic)
    with st.form("nuevo_producto", clear_on_submit=False):
        col1, col2 = st.columns(2)

        with col1:
            nuevo_titulo = st.text_input("Título del Producto", value="")
            nuevo_precio = st.number_input("Precio ($)", min_value=0.50, step=0.50)

        with col2:
            if not isinstance(data.get("categories", {}), dict) or not data["categories"]:
                st.error("No hay categorías en productos.json (data['categories']).")
                st.stop()

            nueva_cat = st.selectbox("Categoría", list(data["categories"].keys()))
            nueva_desc = st.text_area("Descripción", value="")

        st.markdown("#### IA (Preview - opcional)")
        ai_notes = st.text_input("Notas rápidas para la IA (opcional)", value="")
        ai_btn = st.form_submit_button("✨ Generar título/descrición (Preview IA)")
        save_btn = st.form_submit_button("🚀 Guardar y Publicar en GitHub")

    # --- Acción IA Preview ---
    if ai_btn:
        try:
            title_ai, desc_ai = ai_preview_generate(
                category=nueva_cat,
                notes=ai_notes,
                filenames=file_names
            )
            if title_ai:
                st.success("✅ IA (preview) generó un título y descripción.")
                st.session_state["ai_title_suggested"] = title_ai
                st.session_state["ai_desc_suggested"] = desc_ai
            else:
                st.warning("La IA respondió pero no devolvió contenido válido.")
        except Exception as e:
            st.warning(f"⚠️ No se pudo generar con IA (preview): {e}")

    # Si hay sugerencias IA, las mostramos para copiar/pegar (modo preview)
    if "ai_title_suggested" in st.session_state:
        st.markdown("#### Sugerencia IA (copia y pega si quieres)")
        st.text_input("Título sugerido", value=st.session_state.get("ai_title_suggested", ""), key="ai_title_show")
        st.text_area("Descripción sugerida", value=st.session_state.get("ai_desc_suggested", ""), key="ai_desc_show")

    # --- Guardar y publicar ---
    if save_btn:
        # Si el usuario no llenó, y hay sugerencia IA, se puede usar como fallback
        if (not nuevo_titulo.strip()) and st.session_state.get("ai_title_suggested"):
            nuevo_titulo = st.session_state.get("ai_title_suggested", "")

        if (not nueva_desc.strip()) and st.session_state.get("ai_desc_suggested"):
            nueva_desc = st.session_state.get("ai_desc_suggested", "")

        if not nuevo_titulo.strip():
            st.error("Falta el título.")
            st.stop()

        # Guardar imágenes (main + extras)
        mainImg, extraImgs = save_images(uploaded_files, main_choice)
        if not mainImg:
            st.error("No se pudo determinar/guardar la imagen principal.")
            st.stop()

        # Crear ID y objeto
        prod_id = make_product_id(nuevo_titulo)

        nuevo_obj = {
            "title": nuevo_titulo.strip(),
            "price": float(nuevo_precio),
            "description": nueva_desc.strip(),
            "mainImg": mainImg,
            "extraImgs": extraImgs
        }

        # Actualizar JSON
        data["products"][prod_id] = nuevo_obj
        data["categories"][nueva_cat].insert(0, prod_id)

        # Guardado local
        save_data(data)
        st.success("✅ Producto guardado localmente.")

        # Git sync + push automático (branch actual o configurado)
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