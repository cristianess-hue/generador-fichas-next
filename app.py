import streamlit as st
import requests
from bs4 import BeautifulSoup
import re
import json
import io
import os
import time
import zipfile
from google import genai
from google.genai import types

from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as RLImage, Table, TableStyle, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

st.set_page_config(page_title="Generador de Fichas PDF", page_icon="📄", layout="centered")

API_KEY = st.secrets.get("GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY", ""))

# ==========================================
# EXTRACCIÓN ROBUSTA SIN FOTOS DE ASESORES
# ==========================================

def extraer_datos_inmueble(url):
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"
    }
    
    resp = session.get(url, headers=headers, timeout=20)
    resp.encoding = 'utf-8'
    soup = BeautifulSoup(resp.text, 'html.parser')

    og_title = ""
    meta_t = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "twitter:title"})
    if meta_t and meta_t.get("content"):
        og_title = meta_t["content"].strip()
    if not og_title:
        h1 = soup.find("h1")
        og_title = h1.get_text(strip=True) if h1 else ""

    og_desc = ""
    meta_d = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
    if meta_d and meta_d.get("content"):
        og_desc = meta_d["content"].strip()

    for bloque_asesor in soup.find_all(lambda tag: any(cl in str(tag.get('class', '')).lower() or cl in str(tag.get('id', '')).lower() 
                                                      for cl in ['asesor', 'agente', 'agent', 'contact-agent', 'broker', 'perfil-asesor'])):
        bloque_asesor.extract()

    for script in soup(["script", "style", "noscript"]):
        script.extract()
    body_text = soup.get_text(separator="\n", strip=True)

    palabras_prohibidas = [
        "asesor", "agente", "agent", "staff", "perfil", "profile", 
        "team", "realtor", "logo", "avatar", "icon", "user", "banner", "face"
    ]

    imagenes = []
    
    meta_img = soup.find("meta", property="og:image")
    if meta_img and meta_img.get("content"):
        img_url = meta_img["content"]
        if "http" in img_url and not any(p in img_url.lower() for p in palabras_prohibidas):
            imagenes.append(img_url)

    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or img.get("data-original") or ""
        alt = (img.get("alt") or "").lower()
        src_lower = src.lower()
        
        if not src or any(p in src_lower for p in palabras_prohibidas) or any(p in alt for p in palabras_prohibidas):
            continue
            
        if any(term in src_lower for term in ["uploads", "listings", "propiedades", "images", "media"]):
            full_src = re.sub(r'-[0-9]+x[0-9]+', '', src)
            if not full_src.startswith("http"):
                full_src = f"https://nextbr.mx{full_src}"
            if full_src not in imagenes:
                imagenes.append(full_src)

    matches_raw = re.findall(r'https?://[^\s"\']+\.(?:jpe?g|png|webp)', resp.text, re.IGNORECASE)
    for m in matches_raw:
        m_lower = m.lower()
        if ("listings" in m_lower or "uploads" in m_lower) and not any(p in m_lower for p in palabras_prohibidas):
            clean_m = re.sub(r'-[0-9]+x[0-9]+', '', m)
            if clean_m not in imagenes:
                imagenes.append(clean_m)

    return og_title, og_desc, body_text, imagenes

# ==========================================
# PROCESAMIENTO CON GEMINI
# ==========================================

def procesar_con_ia(url, og_title, og_desc, body_text):
    client = genai.Client(api_key=API_KEY)
    
    prompt = f"""
    Eres un analista inmobiliario profesional.
    Extrae la información de la propiedad analizada a partir de los siguientes datos:

    URL: {url}
    Título Meta: {og_title}
    Descripción Meta: {og_desc}
    Texto de la página:
    {body_text[:6000]}

    INSTRUCCIONES ESTRICTAS:
    1. 'nombre_archivo': EXACTAMENTE con la estructura "[Tipo] en [Colonia/Residencial] [Precio]" (Ejemplo: "Casa en Corceles Residencial $17,000" o "Local en Col San Benito $8,500"). Quita cualquier caracter ilegal como / \\ : * ? " < > |.
    2. 'titulo': Nombre formal de la propiedad.
    3. 'precio': Precio formateado con moneda (ej. $17,000 MXN).
    4. 'caracteristicas': Array de hasta 4 puntos clave (ej. ["3 Recámaras", "2 Baños", "180 m² Construcción", "Cochera 2 autos"]).
    5. 'descripcion': ESTRUCTURADA RENGLÓN POR RENGLÓN con viñetas (•).
       - Cada área, recámara, amenidad y equipamiento en una línea separada.
       - Elimina cualquier número telefónico ajeno o nombre de otras empresas.

    Devuelve un JSON estrictamente válido:
    {{
      "titulo": "Título",
      "precio": "$00,000 MXN",
      "caracteristicas": ["..."],
      "
