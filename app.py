import streamlit as st
import requests
from bs4 import BeautifulSoup
import re
import json
import io
import os
import time
import zipfile
from PIL import Image as PILImage
from google import genai
from google.genai import types

from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as RLImage, Table, TableStyle, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

st.set_page_config(page_title="Generador de Fichas PDF", page_icon="📄", layout="centered")

API_KEY = st.secrets.get("GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY", ""))

# ==========================================
# DIRECTORIO DE ASESORES Y MARCA
# ==========================================
LOGO_NEXT_PATH = "logo_next.png"

ASESORES = {
    "Cristian Sosa": {
        "telefono": "6622057331",
        "foto": "cristian_sosa.jpg",
        "lema": "Tu nuevo comienzo empieza en la propiedad correcta."
    },
    "Claudia Castro": {
        "telefono": "6621387957",
        "foto": "claudia_castro.jpg",
        "lema": "Tu tranquilidad es nuestra prioridad: nos encargamos de todo."
    }
}

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
# PROCESAMIENTO CON GEMINI (CASCADA RESILIENTE)
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
      "descripcion": "• ...\\n• ...",
      "nombre_archivo": "Casa en Colonia $00,000"
    }}
    """

    modelos_candidatos = [
        'gemini-2.5-flash-lite',
        'gemini-2.5-flash',
        'gemini-3.5-flash-lite',
        'gemini-3.5-flash'
    ]
    
    ultimo_error = None

    for modelo in modelos_candidatos:
        for intento in range(2):
            try:
                res = client.models.generate_content(
                    model=modelo,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"
                    )
                )
                return json.loads(res.text)
            except Exception as e:
                err_str = str(e)
                ultimo_error = e
                if "503" in err_str or "429" in err_str or "UNAVAILABLE" in err_str:
                    time.sleep(1.5)
                    continue
                if "404" in err_str:
                    break

    raise ultimo_error

# ==========================================
# ESCALADO DE IMÁGENES PROPORCIONAL
# ==========================================

def procesar_imagen_proporcional(fuente, max_w=540, max_h=320, es_local=False):
    try:
        if es_local:
            if not os.path.exists(fuente):
                return None
            with PILImage.open(fuente) as pil_img:
                orig_w, orig_h = pil_img.size
            if orig_w == 0 or orig_h == 0:
                return None
            ratio = min(max_w / orig_w, max_h / orig_h)
            return RLImage(fuente, width=orig_w * ratio, height=orig_h * ratio)
        else:
            headers = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(fuente, headers=headers, timeout=8)
            if r.status_code == 200:
                img_bytes = io.BytesIO(r.content)
                with PILImage.open(img_bytes) as pil_img:
                    orig_w, orig_h = pil_img.size
                if orig_w == 0 or orig_h == 0:
                    return None
                ratio = min(max_w / orig_w, max_h / orig_h)
                img_bytes.seek(0)
                return RLImage(img_bytes, width=orig_w * ratio, height=orig_h * ratio)
    except Exception:
        return None
    return None

# ==========================================
# RENDERIZADO DEL PDF ELEGANTE
# ==========================================

def generar_pdf(titulo, precio, caracteristicas, descripcion, imagenes_urls, asesor_nom, asesor_info):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=28,
        bottomMargin=28
    )

    styles = getSampleStyleSheet()
    
    lema_style = ParagraphStyle(
        'HeaderSlogan',
        parent=styles['Normal'],
        fontName='Helvetica-Oblique',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor('#64748b')
    )

    asesor_header_style = ParagraphStyle(
        'HeaderAsesor',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=10,
        leading=13,
        textColor=colors.HexColor('#0f172a'),
        alignment=2
    )

    asesor_sub_style = ParagraphStyle(
        'HeaderAsesorSub',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor('#0284c7'),
        alignment=2
    )

    title_style = ParagraphStyle(
        'PropTitle',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=16,
        leading=20,
        textColor=colors.HexColor('#0f172a')
    )
    
    price_style = ParagraphStyle(
        'PropPrice',
        parent=styles['Heading2'],
        fontName='Helvetica-Bold',
        fontSize=17,
        leading=21,
        textColor=colors.HexColor('#0284c7')
    )
    
    line_item_style = ParagraphStyle(
        'PropLineItem',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=13.5,
        textColor=colors.HexColor('#334155')
    )

    subhead_style = ParagraphStyle(
        'PropSubHead',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9.5,
        leading=14,
        textColor=colors.HexColor('#0f172a')
    )

    badge_style = ParagraphStyle(
        'PropBadge',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor('#0f172a'),
        alignment=1
    )

    wa_style = ParagraphStyle(
        'PropWA',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=11,
        leading=14,
        textColor=colors.HexColor('#ffffff'),
        alignment=1
    )

    section_style = ParagraphStyle(
        'SectionHeader',
        parent=styles['Heading2'],
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=16,
        textColor=colors.HexColor('#0f172a')
    )

    elementos = []

    # 1. ENCABEZADO CON LOGO, LEMA Y ASESOR
    logo_img = procesar_imagen_proporcional(LOGO_NEXT_PATH, max_w=140, max_h=45, es_local=True)
    foto_asesor_img = procesar_imagen_proporcional(asesor_info.get("foto", ""), max_w=45, max_h=45, es_local=True)

    bloque_logo = []
    if logo_img:
        bloque_logo.append(logo_img)
        bloque_logo.append(Spacer(1, 2))
    bloque_logo.append(Paragraph(asesor_info.get("lema", ""), lema_style))

    bloque_asesor = [
        Paragraph(asesor_nom, asesor_header_style),
        Paragraph(f"📲 {asesor_info.get('telefono', '')}", asesor_sub_style),
        Paragraph("Asesor Inmobiliario Asociado", lema_style)
    ]

    fila_header = [bloque_logo, bloque_asesor]
    col_widths_header = [380, 160]

    if foto_asesor_img:
        fila_header = [bloque_logo, bloque_asesor, foto_asesor_img]
        col_widths_header = [340, 150, 50]

    t_header = Table([fila_header], colWidths=col_widths_header)
    t_header.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('LINEBELOW', (0, 0), (-1, -1), 1, colors.HexColor('#e2e8f0')),
    ]))
    elementos.append(t_header)
    elementos.append(Spacer(1, 8))

    # 2. TÍTULO Y PRECIO
    elementos.append(Paragraph(titulo, title_style))
    elementos.append(Spacer(1, 3))
    if precio:
        elementos.append(Paragraph(precio, price_style))
    elementos.append(Spacer(1, 8))

    # 3. FOTO PRINCIPAL
    fotos_descargadas = []
    for u in imagenes_urls:
        rl_img = procesar_imagen_proporcional(u, max_w=540, max_h=290, es_local=False)
        if rl_img:
            fotos_descargadas.append(rl_img)

    if fotos_descargadas:
        foto_portada = fotos_descargadas[0]
        t_front = Table([[foto_portada]], colWidths=[540])
        t_front.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
        ]))
        elementos.append(t_front)
        elementos.append(Spacer(1, 8))

    # 4. CARACTERÍSTICAS CLAVE
    if caracteristicas:
        badges = [Paragraph(f"✓ {c}", badge_style) for c in caracteristicas[:4]]
        t_badges = Table([badges], colWidths=[135] * len(badges))
        t_badges.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f8fafc')),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
        ]))
        elementos.append(t_badges)
        elementos.append(Spacer(1, 9))

    # 5. DESCRIPCIÓN ESTRUCTURADA
    lineas = [l.strip() for l in descripcion.split("\n") if l.strip()]
    for linea in lineas:
        if linea.endswith(":") or (not linea.startswith("•") and not linea.startswith("-")):
            elementos.append(Spacer(1, 3))
            elementos.append(Paragraph(linea, subhead_style))
        else:
            texto_linea = linea if linea.startswith("•") else f"• {linea.lstrip('-* ')}"
            elementos.append(Paragraph(texto_linea, line_item_style))

    elementos.append(Spacer(1, 12))

    # 6. BOTÓN DE CONTACTO WHATSAPP
    asesor_tel = asesor_info.get("telefono", "")
    wa_url = f"https://wa.me/52{asesor_tel}?text=Hola%20{asesor_nom},%20me%20interesa%20esta%20propiedad:%20{titulo}"
    btn_link = f'<a href="{wa_url}" color="white">📲 Contactar a {asesor_nom} por WhatsApp ({asesor_tel})</a>'
    
    t_btn = Table([[Paragraph(btn_link, wa_style)]], colWidths=[540])
    t_btn.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#25D366')),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    elementos.append(t_btn)

    # 7. GALERÍA DE FOTOS (1 POR FILA)
    resto_fotos = fotos_descargadas[1:]
    if resto_fotos:
        elementos.append(PageBreak())
        elementos.append(Paragraph("📸 Galería de la Propiedad", section_style))
        elementos.append(Spacer(1, 8))

        for foto in resto_fotos:
            t_foto = Table([[foto]], colWidths=[540])
            t_foto.setStyle(TableStyle([
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
                ('TOPPADDING', (0, 0), (-1, -1), 0),
            ]))
            elementos.append(t_foto)
            elementos.append(Spacer(1, 12))

        elementos.append(Spacer(1, 6))
        elementos.append(t_btn)

    doc.build(elementos)
    buffer.seek(0)
    return buffer.getvalue()

# ==========================================
# INTERFAZ STREAMLIT
# ==========================================

st.title("📄 Generador de Fichas en PDF")
st.write("Pega hasta **5 enlaces** de Next Bienes Raíces (uno por línea):")

if "fichas_generadas" not in st.session_state:
    st.session_state.fichas_generadas = []
if "zip_buffer" not in st.session_state:
    st.session_state.zip_buffer = None

urls_raw = st.text_area(
    "Enlaces de las propiedades:",
    value="",
    placeholder="https://nextbr.mx/propiedades/ejemplo-propiedad-1\nhttps://nextbr.mx/propiedades/ejemplo-propiedad-2",
    height=120
)

col1, col2 = st.columns(2)
with col1:
    asesor_nombre = st.selectbox("Selecciona Asesor:", options=list(ASESORES.keys()), index=0)

info_asesor = ASESORES.get(asesor_nombre, {})

with col2:
    st.text_input("WhatsApp:", value=info_asesor.get("telefono", ""), disabled=True)

st.caption(f"**Lema asignado:** _{info_asesor.get('lema', '')}_")

st.markdown("---")
btn_generar = st.button("🚀 Iniciar Generación de PDFs", type="primary", use_container_width=True)

if btn_generar:
    if not API_KEY:
        st.error("No se detectó la clave GEMINI_API_KEY en Streamlit Secrets.")
    else:
        lista_urls = [u.strip() for u in urls_raw.strip().split("\n") if u.strip().startswith("http")]
        
        if not lista_urls:
            st.error("Pega al menos un enlace válido.")
        else:
            if len(lista_urls) > 5:
                st.warning("Se procesarán los primeros 5 enlaces.")
                lista_urls = lista_urls[:5]
            
            st.session_state.fichas_generadas = []
            st.session_state.zip_buffer = None

            barra = st.progress(0)
            total = len(lista_urls)

            for idx, url in enumerate(lista_urls):
                with st.spinner(f"Procesando {idx + 1} de {total}: {url.split('/')[-1]}..."):
                    try:
                        og_title, og_desc, body_text, fotos = extraer_datos_inmueble(url)
                        info = procesar_con_ia(url, og_title, og_desc, body_text)

                        titulo = info.get("titulo", "Propiedad Inmobiliaria")
                        precio = info.get("precio", "")
                        tags = info.get("caracteristicas", [])
                        desc_final = info.get("descripcion", "")
                        
                        nombre_base = info.get("nombre_archivo", titulo)
                        nombre_limpio = re.sub(r'[\\/*?:"<>|]', "", nombre_base).strip()
                        nombre_pdf = f"{nombre_limpio}.pdf"

                        pdf_data = generar_pdf(
                            titulo=titulo,
                            precio=precio,
                            caracteristicas=tags,
                            descripcion=desc_final,
                            imagenes_urls=fotos,
                            asesor_nom=asesor_nombre,
                            asesor_info=info_asesor
                        )

                        st.session_state.fichas_generadas.append({
                            "nombre": nombre_pdf,
                            "datos": pdf_data,
                            "fotos_count": len(fotos)
                        })

                    except Exception as e:
                        st.error(f"Error en {url}: {e}")

                barra.progress((idx + 1) / total)

            if st.session_state.fichas_generadas:
                zip_io = io.BytesIO()
                with zipfile.ZipFile(zip_io, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                    for item in st.session_state.fichas_generadas:
                        zf.writestr(item["nombre"], item["datos"])
                zip_io.seek(0)
                st.session_state.zip_buffer = zip_io.getvalue()
                st.balloons()

if st.session_state.fichas_generadas:
    st.subheader("🎉 Fichas Listas")
    
    if st.session_state.zip_buffer:
        st.download_button(
            label="📦 Descargar TODAS las fichas en un archivo ZIP",
            data=st.session_state.zip_buffer,
            file_name="Fichas_Inmobiliarias.zip",
            mime="application/zip",
            type="primary",
            use_container_width=True
        )

    st.markdown("---")
    st.write("**O descárgalas individualmente si prefieres:**")
    
    for i, ficha in enumerate(st.session_state.fichas_generadas):
        col_txt, col_btn = st.columns([3, 2])
        with col_txt:
            st.write(f"📄 **{ficha['nombre']}** ({ficha['fotos_count']} fotos)")
        with col_btn:
            st.download_button(
                label=f"📥 Descargar PDF",
                data=ficha["datos"],
                file_name=ficha["nombre"],
                mime="application/pdf",
                key=f"dl_persist_{i}",
                use_container_width=True
            )
