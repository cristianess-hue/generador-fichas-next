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
from reportlab.pdfgen import canvas

st.set_page_config(page_title="Generador de Fichas PDF", page_icon="📄", layout="centered")

API_KEY = st.secrets.get("GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY", ""))

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
        "lema": "Conectamos personas con los espacios que sueñan."
    }
}

# ==========================================
# EXTRACCIÓN Y SCRAPING
# ==========================================

def extraer_datos_inmueble(url):
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "es-ES,es;q=0.9"
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

    for bloque in soup.find_all(lambda t: any(cl in str(t.get('class', '')).lower() or cl in str(t.get('id', '')).lower() 
                                            for cl in ['asesor', 'agente', 'agent', 'broker', 'perfil-asesor'])):
        bloque.extract()

    for s in soup(["script", "style", "noscript"]):
        s.extract()
    body_text = soup.get_text(separator="\n", strip=True)

    palabras_prohibidas = ["asesor", "agente", "agent", "staff", "perfil", "team", "logo", "avatar", "icon", "user"]
    imagenes = []

    meta_img = soup.find("meta", property="og:image")
    if meta_img and meta_img.get("content"):
        img_url = meta_img["content"]
        if "http" in img_url and not any(p in img_url.lower() for p in palabras_prohibidas):
            imagenes.append(img_url)

    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        alt = (img.get("alt") or "").lower()
        if not src or any(p in src.lower() for p in palabras_prohibidas) or any(p in alt for p in palabras_prohibidas):
            continue
        if any(term in src.lower() for term in ["uploads", "listings", "propiedades", "images"]):
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

    # Extraer ID / Folio de la URL si existe
    folio_match = re.search(r'-(\d{4,6})$', url.rstrip('/'))
    folio = folio_match.group(1) if folio_match else "S/F"

    return og_title, og_desc, body_text, imagenes, folio

# ==========================================
# ESTRUCTURACIÓN CON IA (CATEGORIZADA Y SIN ALUCINACIONES)
# ==========================================

def procesar_con_ia(url, og_title, og_desc, body_text):
    client = genai.Client(api_key=API_KEY)
    
    prompt = f"""
    Eres un analista inmobiliario profesional. Extrae la información verificable de la propiedad:
    URL: {url}
    Título Meta: {og_title}
    Descripción Meta: {og_desc}
    Texto:
    {body_text[:6500]}

    REGLAS ESTRICTAS (CERO ALUCINACIONES):
    1. 'titulo': Título formal en MAYÚSCULAS o Capitalizado.
    2. 'precio': Formato con moneda (ej. $3,580,000.00 MXN).
    3. 'caracteristicas': Array con EXACTAMENTE estos datos si existen en el texto:
       - Recámaras (ej: "3 Recámaras")
       - Baños (ej: "2.5 Baños")
       - Terreno (ej: "148 m² Terreno")
       - Construcción (ej: "192 m² Construcción")
    4. 'estatus_legal': Si el texto dice "libre de gravamen", pon "Libre de Gravamen". Si no dice nada, déjalo vacío "".
    5. 'secciones': Separa la descripción en las secciones que apliquen:
       - "distribucion_pb": lista de amenidades de Planta Baja (sin inventar).
       - "distribucion_pa": lista de amenidades de Planta Alta (sin inventar).
       - "equipamiento": lista de equipamiento (ej. Minisplits, Paneles solares, Cocina integral, etc.).
       - "area_comun": amenidades de la privada o residencial (parque, alberca, canchas, etc.).
    6. 'nombre_archivo': Estructura "[Tipo] en [Colonia] [Precio]" sin caracteres ilegales.

    Responde en JSON estrictamente:
    {{
      "titulo": "...",
      "precio": "...",
      "caracteristicas": ["..."],
      "estatus_legal": "...",
      "secciones": {{
        "distribucion_pb": ["..."],
        "distribucion_pa": ["..."],
        "equipamiento": ["..."],
        "area_comun": ["..."]
      }},
      "nombre_archivo": "..."
    }}
    """

    modelos_candidatos = ['gemini-2.5-flash-lite', 'gemini-2.5-flash', 'gemini-3.5-flash-lite']
    ultimo_error = None

    for modelo in modelos_candidatos:
        for intento in range(2):
            try:
                res = client.models.generate_content(
                    model=modelo,
                    contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json")
                )
                return json.loads(res.text)
            except Exception as e:
                ultimo_error = e
                time.sleep(1.5)
                continue

    raise ultimo_error

# ==========================================
# CÁLCULO EXACTO DE PRECIO POR M²
# ==========================================

def calcular_precio_m2(precio_str, caracteristicas_list):
    try:
        precio_num = float(re.sub(r'[^\d.]', '', precio_str))
        m2_const = None
        for c in caracteristicas_list:
            if "construcci" in c.lower() or "const" in c.lower():
                m = re.search(r'(\d+(?:\.\d+)?)', c)
                if m:
                    m2_const = float(m.group(1))
                    break
        if m2_const and m2_const > 0 and precio_num > 50000:  # Solo para venta o valores reales
            pm2 = precio_num / m2_const
            return f"${pm2:,.0f} MXN/m²"
    except Exception:
        pass
    return None

# ==========================================
# ESCALADO DE IMÁGENES
# ==========================================

def procesar_imagen_proporcional(fuente, max_w=540, max_h=300, es_local=False):
    try:
        if es_local:
            if not os.path.exists(fuente):
                return None
            with PILImage.open(fuente) as p_img:
                w, h = p_img.size
            ratio = min(max_w / w, max_h / h)
            return RLImage(fuente, width=w * ratio, height=h * ratio)
        else:
            headers = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(fuente, headers=headers, timeout=8)
            if r.status_code == 200:
                b = io.BytesIO(r.content)
                with PILImage.open(b) as p_img:
                    w, h = p_img.size
                ratio = min(max_w / w, max_h / h)
                b.seek(0)
                return RLImage(b, width=w * ratio, height=h * ratio)
    except Exception:
        return None
    return None

# ==========================================
# NUMERACIÓN DE PÁGINA (CANVAS NUMERADO)
# ==========================================

class NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_footer(num_pages)
            super().showPage()
        super().save()

    def draw_footer(self, page_count):
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#94a3b8"))
        # Pie de página elegante a 18 puntos del fondo
        texto_pag = f"Página {self._pageNumber} de {page_count}"
        self.drawRightString(576, 18, texto_pag)
        self.drawString(36, 18, "Next Bienes Raíces • Ficha Técnica de Propiedad")
        self.restoreState()

# ==========================================
# RENDERIZADO DEL PDF PREMIUM
# ==========================================

def generar_pdf(info, imagenes_urls, asesor_nom, asesor_info, folio):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=26,
        bottomMargin=34
    )

    styles = getSampleStyleSheet()

    lema_style = ParagraphStyle('H_Lema', parent=styles['Normal'], fontName='Helvetica-Oblique', fontSize=8, leading=10, textColor=colors.HexColor('#64748b'))
    asesor_h = ParagraphStyle('H_Asesor', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=10, leading=12, textColor=colors.HexColor('#0f172a'), alignment=2)
    asesor_s = ParagraphStyle('H_Tel', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=8.5, leading=11, textColor=colors.HexColor('#0284c7'), alignment=2)
    
    title_style = ParagraphStyle('Title', parent=styles['Heading1'], fontName='Helvetica-Bold', fontSize=15, leading=18, textColor=colors.HexColor('#0f172a'))
    price_style = ParagraphStyle('Price', parent=styles['Heading2'], fontName='Helvetica-Bold', fontSize=16, leading=19, textColor=colors.HexColor('#0284c7'))
    folio_style = ParagraphStyle('Folio', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=9, leading=12, textColor=colors.HexColor('#64748b'), alignment=2)
    
    badge_style = ParagraphStyle('Badge', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=8, leading=10, textColor=colors.HexColor('#0f172a'), alignment=1)
    legal_style = ParagraphStyle('Legal', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=8.5, leading=11, textColor=colors.HexColor('#166534'), alignment=1)
    
    subhead_style = ParagraphStyle('Subhead', parent=styles['Heading3'], fontName='Helvetica-Bold', fontSize=9.5, leading=13, textColor=colors.HexColor('#0f172a'))
    item_style = ParagraphStyle('Item', parent=styles['Normal'], fontName='Helvetica', fontSize=8.5, leading=12, textColor=colors.HexColor('#334155'))
    
    wa_style = ParagraphStyle('WA', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=10.5, leading=13, textColor=colors.HexColor('#ffffff'), alignment=1)
    sec_title_style = ParagraphStyle('SecTitle', parent=styles['Heading2'], fontName='Helvetica-Bold', fontSize=12, leading=15, textColor=colors.HexColor('#0f172a'))

    elementos = []

    # 1. ENCABEZADO
    logo_img = procesar_imagen_proporcional(LOGO_NEXT_PATH, max_w=130, max_h=40, es_local=True)
    foto_asesor_img = procesar_imagen_proporcional(asesor_info.get("foto", ""), max_w=42, max_h=42, es_local=True)

    bloque_logo = []
    if logo_img:
        bloque_logo.append(logo_img)
        bloque_logo.append(Spacer(1, 2))
    bloque_logo.append(Paragraph(asesor_info.get("lema", ""), lema_style))

    bloque_asesor = [
        Paragraph(asesor_nom, asesor_h),
        Paragraph(f"📲 {asesor_info.get('telefono', '')}", asesor_s),
        Paragraph("Asesor Inmobiliario Asociado", lema_style)
    ]

    fila_header = [bloque_logo, bloque_asesor]
    col_widths = [380, 160]
    if foto_asesor_img:
        fila_header = [bloque_logo, bloque_asesor, foto_asesor_img]
        col_widths = [340, 150, 50]

    t_header = Table([fila_header], colWidths=col_widths)
    t_header.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LINEBELOW', (0, 0), (-1, -1), 1, colors.HexColor('#e2e8f0')),
    ]))
    elementos.append(t_header)
    elementos.append(Spacer(1, 6))

    # 2. TÍTULO, PRECIO Y FOLIO
    elementos.append(Paragraph(info.get("titulo", "Propiedad en Venta"), title_style))
    elementos.append(Spacer(1, 2))

    t_precio_folio = Table([
        [Paragraph(info.get("precio", ""), price_style), Paragraph(f"ID / Folio: #{folio}", folio_style)]
    ], colWidths=[380, 160])
    t_precio_folio.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
    ]))
    elementos.append(t_precio_folio)
    elementos.append(Spacer(1, 6))

    # 3. FOTO DE PORTADA
    fotos_desc = []
    for u in imagenes_urls:
        im = procesar_imagen_proporcional(u, max_w=540, max_h=260, es_local=False)
        if im:
            fotos_desc.append(im)

    if fotos_desc:
        t_front = Table([[fotos_desc[0]]], colWidths=[540])
        t_front.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
        ]))
        elementos.append(t_front)
        elementos.append(Spacer(1, 6))

    # 4. CARACTERÍSTICAS TÉCNICAS + CÁLCULO DE M²
    caracts = list(info.get("caracteristicas", []))
    pm2 = calcular_precio_m2(info.get("precio", ""), caracts)
    if pm2:
        caracts.append(pm2)

    if caracts:
        badges = [Paragraph(f"✓ {c}", badge_style) for c in caracts[:5]]
        ancho_col = 540 / len(badges)
        t_badges = Table([badges], colWidths=[ancho_col] * len(badges))
        t_badges.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f8fafc')),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
        ]))
        elementos.append(t_badges)
        elementos.append(Spacer(1, 6))

    # 5. ESTATUS JURÍDICO DESTACADO
    if info.get("estatus_legal"):
        t_legal = Table([[Paragraph(f"🛡️ Estatus Jurídico: {info['estatus_legal']} • Acepta créditos bancarios y recursos propios", legal_style)]], colWidths=[540])
        t_legal.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#dcfce7')),
            ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#86efac')),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ]))
        elementos.append(t_legal)
        elementos.append(Spacer(1, 6))

    # 6. DISTRIBUCIÓN Y EQUIPAMIENTO CATEGORIZADOS
    sec = info.get("secciones", {})
    bloques_desc = [
        ("🔹 Planta Baja:", sec.get("distribucion_pb", [])),
        ("🔹 Planta Alta:", sec.get("distribucion_pa", [])),
        ("⚡ Equipamiento Incluido:", sec.get("equipamiento", [])),
        ("🌳 Privada y Áreas Comunes:", sec.get("area_comun", []))
    ]

    for titulo_sec, items in bloques_desc:
        if items:
            elementos.append(Paragraph(titulo_sec, subhead_style))
            texto_linea = " • ".join(items)
            elementos.append(Paragraph(f"• {texto_linea}", item_style))
            elementos.append(Spacer(1, 3))

    elementos.append(Spacer(1, 6))

    # 7. BOTÓN WHATSAPP DE PORTADA
    wa_num = asesor_info.get("telefono", "")
    wa_url = f"https://wa.me/52{wa_num}?text=Hola%20{asesor_nom},%20me%20interesa%20la%20propiedad:%20{info.get('titulo', '')}%20(Folio:%20{folio})"
    btn_markup = f'<a href="{wa_url}" color="white">📲 Contactar a {asesor_nom} por WhatsApp ({wa_num})</a>'

    t_btn = Table([[Paragraph(btn_markup, wa_style)]], colWidths=[540])
    t_btn.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#25D366')),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 7),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
    ]))
    elementos.append(t_btn)

    # 8. GALERÍA DE FOTOS (1 POR FILA)
    resto_fotos = fotos_desc[1:]
    if resto_fotos:
        elementos.append(PageBreak())
        elementos.append(Paragraph("📸 Galería Fotográfica de la Propiedad", sec_title_style))
        elementos.append(Spacer(1, 8))

        for f in resto_fotos:
            t_f = Table([[f]], colWidths=[540])
            t_f.setStyle(TableStyle([
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
                ('TOPPADDING', (0, 0), (-1, -1), 0),
            ]))
            elementos.append(t_f)
            elementos.append(Spacer(1, 10))

        elementos.append(Spacer(1, 4))
        elementos.append(t_btn)

    doc.build(elementos, canvasmaker=NumberedCanvas)
    buffer.seek(0)
    return buffer.getvalue()

# ==========================================
# INTERFAZ WEB STREAMLIT
# ==========================================

st.title("📄 Generador de Fichas PDF Premium")
st.write("Pega hasta **5 enlaces** de Next Bienes Raíces (uno por línea):")

if "fichas_generadas" not in st.session_state:
    st.session_state.fichas_generadas = []
if "zip_buffer" not in st.session_state:
    st.session_state.zip_buffer = None

urls_raw = st.text_area(
    "Enlaces de las propiedades:",
    value="",
    placeholder="https://nextbr.mx/propiedades/casa-en-venta-en-montecarlo-residencial-34759",
    height=110
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
                        og_title, og_desc, body_text, fotos, folio = extraer_datos_inmueble(url)
                        info = procesar_con_ia(url, og_title, og_desc, body_text)

                        nombre_base = info.get("nombre_archivo", info.get("titulo", "Propiedad"))
                        nombre_limpio = re.sub(r'[\\/*?:"<>|]', "", nombre_base).strip()
                        nombre_pdf = f"{nombre_limpio}.pdf"

                        pdf_data = generar_pdf(
                            info=info,
                            imagenes_urls=fotos,
                            asesor_nom=asesor_nombre,
                            asesor_info=info_asesor,
                            folio=folio
                        )

                        st.session_state.fichas_generadas.append({
                            "nombre": nombre_pdf,
                            "datos": pdf_data,
                            "fotos_count": len(fotos)
                        })
                    except Exception as e:
                        st.error(f"Error procesando {url}: {e}")

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
            file_name="Fichas_Next_Inmobiliarias.zip",
            mime="application/zip",
            type="primary",
            use_container_width=True
        )

    st.markdown("---")
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
                key=f"dl_f_{i}",
                use_container_width=True
            )
